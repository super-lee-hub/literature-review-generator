"""Build auditable inputs for the frozen 84-paper PPH Stage 1 parent.

The module is intentionally provider-free.  It proves which existing Stage 1
summaries are reusable from valid Registry workspaces and prepares fresh local
full-text evidence for the frozen PDF set.  Model/subagent output is imported
only by later materialization steps.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from preprocess.service import PreprocessManager
from runtime.job_spec import load_runtime_job_spec
from runtime.orchestrator import AgentRuntimeBridge
from runtime.reconcile import RuntimeReconciler, validate_canonical_ai_summary
from runtime.runner import AgentRuntimeRunner
from runtime.stage_contracts import PaperWorkItem
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_workspace import JobWorkspace, atomic_write_json, utc_now_iso
from services.stage1_input_completeness import is_blocked_stage1_quality
from services.summary_reuse import SummaryCatalog, SummarySource, collect_summary_sources
from summary_schema import normalize_ai_summary

from scripts.pph_stage1_rebuild import (
    KALYANARAM_CANONICAL_KEY,
    KALYANARAM_SUMMARY_SHA256,
    PARENT_SPEC_NAME,
    SELECTED_MANIFEST_NAME,
    audit_bundle,
)


COVERAGE_SCHEMA = "pph-stage1-registered-summary-coverage-v1"
EVIDENCE_INDEX_SCHEMA = "pph-stage1-current-pdf-evidence-v1"
MATERIALIZATION_SCHEMA = "pph-stage1-parent-materialization-v1"
GENERATION_REQUEST_SCHEMA = "pph-stage1-subagent-request-v1"
GENERATION_MANIFEST_SCHEMA = "pph-stage1-subagent-request-manifest-v1"
COVERAGE_REPORT_NAME = "stage1_registered_coverage_audit.json"
EVIDENCE_INDEX_NAME = "stage1_current_pdf_evidence_index.json"
MATERIALIZATION_REPORT_NAME = "stage1_parent_materialization_report.json"
GENERATION_REQUEST_MANIFEST_NAME = "stage1_subagent_request_manifest.json"
EXPECTED_CORPUS_COUNT = 84
FORBIDDEN_SOURCE_MARKERS = (
    "pph_supplemental_kalyanaram_reference_price__20260729_101532_3e654ed3",
)
STANDARD_TESSERACT_DIR = Path(r"C:\Program Files\Tesseract-OCR")
OCR_MODES = frozenset({"off", "auto", "always"})
WORK_LOCK_NAME = ".prepare-evidence.lock"


class Stage1ParentError(RuntimeError):
    """Raised when the parent cannot be built without weakening provenance."""


def _read_json(path: str | Path) -> Any:
    target = Path(path).expanduser().resolve()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Stage1ParentError(f"cannot read JSON artifact: {target}") from exc


def _read_summary_records(path: str | Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if isinstance(payload, Mapping) and isinstance(payload.get("summaries"), list):
        payload = payload["summaries"]
    if not isinstance(payload, list):
        raise Stage1ParentError(f"summary artifact must be a JSON array: {path}")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise Stage1ParentError(f"summary record {index} is not an object: {path}")
        records.append(dict(item))
    return records


def _paper_key(summary: Mapping[str, Any]) -> str:
    paper_info = summary.get("paper_info")
    if not isinstance(paper_info, Mapping):
        return ""
    return str(paper_info.get("canonical_paper_key") or "").strip()


def _selected_rows(path: str | Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise Stage1ParentError("selected source manifest is not an object")
    rows = payload.get("selected_sources")
    if not isinstance(rows, list) or len(rows) != EXPECTED_CORPUS_COUNT:
        raise Stage1ParentError(
            f"selected source manifest must contain {EXPECTED_CORPUS_COUNT} rows"
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            raise Stage1ParentError(f"selected source row {index} is not an object")
        row = dict(raw_row)
        key = str(row.get("canonical_paper_key") or "").strip()
        pdf_hash = str(row.get("pdf_sha256") or "").strip().lower()
        selected_pdf = str(row.get("selected_pdf_path") or "").strip()
        if not key or key in seen:
            raise Stage1ParentError(
                f"selected source row {index} has an invalid canonical identity"
            )
        if len(pdf_hash) != 64 or any(char not in "0123456789abcdef" for char in pdf_hash):
            raise Stage1ParentError(
                f"selected source row {index} has an invalid PDF SHA-256"
            )
        if not selected_pdf:
            raise Stage1ParentError(
                f"selected source row {index} has no selected PDF path"
            )
        seen.add(key)
        normalized.append(row)
    return normalized


def _workspace_parts(
    summary_path: Path,
    *,
    output_root: Path,
) -> tuple[Path, str, str]:
    if summary_path.parent.name != "artifacts":
        raise Stage1ParentError("summary is not inside a canonical artifacts directory")
    workspace_root = summary_path.parent.parent.resolve()
    try:
        workspace_root.relative_to(output_root.resolve())
    except ValueError as exc:
        raise Stage1ParentError("summary workspace is outside the output root") from exc
    registry_path = workspace_root / "artifact_registry.json"
    registry_payload = _read_json(registry_path)
    if not isinstance(registry_payload, Mapping):
        raise Stage1ParentError("artifact Registry is not an object")
    job_id = str(registry_payload.get("job_id") or "").strip()
    suffix = f"__{job_id}"
    if not job_id or not workspace_root.name.endswith(suffix):
        raise Stage1ParentError("workspace name and Registry job_id do not agree")
    project_name = workspace_root.name[: -len(suffix)]
    if not project_name:
        raise Stage1ParentError("workspace project name is empty")
    return workspace_root, project_name, job_id


def validate_registered_summary_source(
    source: SummarySource,
    *,
    output_root: str | Path,
) -> dict[str, Any]:
    """Validate one canonical summary file and its exact Registry record."""

    summary_path = Path(source.path).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    if any(marker in str(summary_path) for marker in FORBIDDEN_SOURCE_MARKERS):
        raise Stage1ParentError("source is retained only as known defect evidence")
    if not summary_path.is_file():
        raise Stage1ParentError("summary file is missing")

    workspace_root, project_name, job_id = _workspace_parts(
        summary_path,
        output_root=output,
    )
    registry_path = workspace_root / "artifact_registry.json"
    registry = ArtifactRegistry(registry_path, job_id)
    records = [
        record
        for record in registry.list_records()
        if Path(record.path).expanduser().resolve() == summary_path
    ]
    if len(records) != 1:
        raise Stage1ParentError(
            f"summary file must have exactly one Registry record, got {len(records)}"
        )
    record = records[0]
    if record.artifact_type != "summary_file" or record.status != "ready":
        raise Stage1ParentError(
            f"summary Registry record is not ready summary_file: "
            f"{record.artifact_type}/{record.status}"
        )
    actual_hash = file_sha256(summary_path)
    if record.content_hash != actual_hash:
        raise Stage1ParentError("summary Registry hash does not match file bytes")

    workspace = JobWorkspace(output, project_name, job_id)
    if Path(workspace.root_dir).resolve() != workspace_root:
        raise Stage1ParentError("reconstructed workspace path does not match source")
    RuntimeReconciler(workspace, registry).validate_record(record, registry=registry)
    return {
        "path": str(summary_path),
        "summary_sha256": actual_hash,
        "workspace_path": str(workspace_root),
        "registry_path": str(registry_path),
        "job_id": job_id,
        "project_name": project_name,
        "artifact_id": record.artifact_id,
        "artifact_type": record.artifact_type,
        "artifact_status": record.status,
        "source_type": source.source_type,
        "source_label": source.label,
        "source_priority": source.priority,
    }


def audit_registered_summary_coverage(
    *,
    selected_manifest_path: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    """Resolve the frozen corpus only against valid registered summary files."""

    selected_manifest = Path(selected_manifest_path).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    rows = _selected_rows(selected_manifest)
    discovered = collect_summary_sources(
        explicit_paths=None,
        output_root=output,
        current_workspace_root=None,
        current_summary_file=None,
    )

    valid_sources: list[SummarySource] = []
    source_receipts: dict[str, dict[str, Any]] = {}
    rejected_sources: list[dict[str, Any]] = []
    for source in discovered:
        try:
            receipt = validate_registered_summary_source(
                source,
                output_root=output,
            )
        except Exception as exc:
            rejected_sources.append(
                {
                    "path": source.path,
                    "source_type": source.source_type,
                    "source_label": source.label,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        valid_sources.append(source)
        source_receipts[str(Path(source.path).resolve())] = receipt

    catalog = SummaryCatalog.from_sources(valid_sources)
    covered: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    invalid_candidates: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["canonical_paper_key"])
        match = catalog.resolve_for_paper(row)
        if match is None or match.winner is None:
            missing.append(
                {
                    "canonical_paper_key": key,
                    "title": str(row.get("title") or ""),
                    "pdf_sha256": str(row["pdf_sha256"]),
                }
            )
            continue
        if match.is_ambiguous:
            ambiguous.append(
                {
                    "canonical_paper_key": key,
                    "title": str(row.get("title") or ""),
                    "candidate_count": len(match.ambiguous_candidates),
                    "candidate_paths": [
                        candidate.source.path
                        for candidate in match.ambiguous_candidates
                    ],
                }
            )
            continue
        winner = match.winner
        try:
            validate_canonical_ai_summary(
                winner.summary.get("ai_summary"),
                label=f"registered Stage 1 summary {key}",
            )
        except Exception as exc:
            invalid_candidates.append(
                {
                    "canonical_paper_key": key,
                    "title": str(row.get("title") or ""),
                    "source_path": winner.source.path,
                    "record_index": winner.record_index,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        receipt = source_receipts[str(Path(winner.source.path).resolve())]
        covered.append(
            {
                "canonical_paper_key": key,
                "title": str(row.get("title") or ""),
                "pdf_sha256": str(row["pdf_sha256"]),
                "match_type": match.match_type,
                "source_path": winner.source.path,
                "source_record_index": winner.record_index,
                "source_summary_sha256": receipt["summary_sha256"],
                "source_job_id": receipt["job_id"],
                "source_artifact_id": receipt["artifact_id"],
                "source_registry_path": receipt["registry_path"],
            }
        )

    kalyanaram = next(
        (
            item
            for item in covered
            if item["canonical_paper_key"] == KALYANARAM_CANONICAL_KEY
        ),
        None,
    )
    if (
        kalyanaram is None
        or kalyanaram["source_summary_sha256"] != KALYANARAM_SUMMARY_SHA256
    ):
        raise Stage1ParentError(
            "coverage audit did not select the canonical Kalyanaram summary"
        )

    covered_keys = {str(item["canonical_paper_key"]) for item in covered}
    missing_keys = {str(item["canonical_paper_key"]) for item in missing}
    ambiguous_keys = {str(item["canonical_paper_key"]) for item in ambiguous}
    invalid_keys = {
        str(item["canonical_paper_key"]) for item in invalid_candidates
    }
    if (
        len(covered_keys | missing_keys | ambiguous_keys | invalid_keys)
        != EXPECTED_CORPUS_COUNT
        or any(
            left & right
            for left, right in (
                (covered_keys, missing_keys),
                (covered_keys, ambiguous_keys),
                (covered_keys, invalid_keys),
                (missing_keys, ambiguous_keys),
                (missing_keys, invalid_keys),
                (ambiguous_keys, invalid_keys),
            )
        )
    ):
        raise Stage1ParentError("coverage audit did not partition the frozen corpus")

    status = (
        "clean"
        if not missing and not ambiguous and not invalid_candidates
        else "findings"
    )
    return {
        "artifact_type": "stage1_registered_summary_coverage",
        "artifact_version": "v1",
        "schema_version": COVERAGE_SCHEMA,
        "created_at": utc_now_iso(),
        "status": status,
        "provider_executed": False,
        "selected_manifest_path": str(selected_manifest),
        "selected_manifest_sha256": file_sha256(selected_manifest),
        "output_root": str(output),
        "expected_corpus_count": EXPECTED_CORPUS_COUNT,
        "discovered_source_count": len(discovered),
        "valid_registered_source_count": len(valid_sources),
        "rejected_source_count": len(rejected_sources),
        "covered_count": len(covered),
        "missing_count": len(missing),
        "ambiguous_count": len(ambiguous),
        "invalid_candidate_count": len(invalid_candidates),
        "ready_to_materialize_without_new_analysis": status == "clean",
        "covered": covered,
        "missing": missing,
        "ambiguous": ambiguous,
        "invalid_candidates": invalid_candidates,
        "valid_registered_sources": sorted(
            source_receipts.values(),
            key=lambda item: (item["source_priority"], item["path"]),
        ),
        "rejected_sources": rejected_sources,
        "catalog_rejected_candidates": catalog.rejected_candidates,
    }


def _ensure_tesseract_on_path() -> str:
    executable = STANDARD_TESSERACT_DIR / "tesseract.exe"
    if not executable.is_file():
        return ""
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    standard = str(STANDARD_TESSERACT_DIR)
    if standard.casefold() not in {part.casefold() for part in path_parts}:
        os.environ["PATH"] = os.pathsep.join([standard, *path_parts])
    return str(executable)


def _local_preprocess_config(
    cache_root: Path,
    *,
    ocr_mode: str = "off",
) -> dict[str, Any]:
    return {
        "Paths": {"output_path": str(cache_root.parent)},
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(cache_root),
            "parser_mode": "local",
            "primary_parser": "local",
            "fallback_parser": "local",
            "extractor_profile": "fitz",
            "ocr_mode": ocr_mode,
            "ocr_languages": "eng",
            "force_rebuild": "false",
            "enable_local_rag": "false",
            "use_markdown_as_stage1_input": "true",
            "retain_structured_output": "true",
            "retain_page_index": "true",
            "retain_diagnostics": "true",
        },
    }


def _evidence_entry_valid(
    entry: Mapping[str, Any],
    *,
    ocr_mode: str = "off",
) -> bool:
    recorded_mode = str(entry.get("ocr_mode") or "").strip().lower()
    if recorded_mode and recorded_mode != ocr_mode:
        return False
    if ocr_mode != "off" and recorded_mode != ocr_mode:
        return False
    if ocr_mode == "off" and bool(entry.get("used_ocr")):
        return False
    expected = {
        "markdown_path": "markdown_sha256",
        "chunks_path": "chunks_sha256",
        "page_index_path": "page_index_sha256",
        "stage1_input_path": "stage1_input_sha256",
        "stage1_input_manifest_path": "stage1_input_manifest_sha256",
        "stage1_quality_report_path": "stage1_quality_report_sha256",
    }
    for path_field, hash_field in expected.items():
        path = Path(str(entry.get(path_field) or ""))
        expected_hash = str(entry.get(hash_field) or "")
        if not path.is_file() or len(expected_hash) != 64:
            return False
        if file_sha256(path) != expected_hash:
            return False
    source_pdf = Path(str(entry.get("source_pdf") or ""))
    source_hash = str(entry.get("source_pdf_sha256") or "")
    return (
        source_pdf.is_file()
        and len(source_hash) == 64
        and file_sha256(source_pdf) == source_hash
    )


def _normalize_reused_evidence_entry(
    entry: Mapping[str, Any],
    *,
    ocr_mode: str,
) -> dict[str, Any]:
    quality_report = _read_json(str(entry.get("stage1_quality_report_path") or ""))
    if not isinstance(quality_report, Mapping):
        raise Stage1ParentError("stage1 quality report is not an object")
    quality_level = str(quality_report.get("stage1_quality_level") or "").strip()
    raw_reasons = quality_report.get("stage1_quality_reasons")
    if not quality_level or not isinstance(raw_reasons, list):
        raise Stage1ParentError("stage1 quality report is missing its decision fields")
    quality_reasons = [str(reason) for reason in raw_reasons]
    normalized = dict(entry)
    normalized.update(
        {
            "ocr_mode": ocr_mode,
            "stage1_quality_level": quality_level,
            "stage1_quality_reasons": quality_reasons,
            "blocked": is_blocked_stage1_quality(
                quality_level,
                quality_reasons,
            ),
        }
    )
    return normalized


def _atomic_write_json_with_retry(
    path: Path,
    payload: Mapping[str, Any],
    *,
    attempts: int = 5,
) -> None:
    for attempt in range(attempts):
        try:
            atomic_write_json(str(path), dict(payload))
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.1 * (attempt + 1))


@contextmanager
def _exclusive_work_lock(work_dir: Path) -> Iterator[None]:
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / WORK_LOCK_NAME
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - Windows is the production target for this task.
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise Stage1ParentError(
            f"prepare-evidence already owns this work directory: {work_dir}"
        ) from exc

    try:
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the production target.
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _evidence_entry_from_result(
    *,
    result: Any,
    order: int,
    row: Mapping[str, Any],
    item: PaperWorkItem,
    source_pdf: Path,
    expected_hash: str,
    ocr_mode: str,
) -> dict[str, Any]:
    quality_report = _read_json(result.stage1_quality_report_path)
    quality_reasons = [
        str(reason)
        for reason in (
            quality_report.get("stage1_quality_reasons")
            if isinstance(quality_report, Mapping)
            else []
        )
        or []
    ]
    blocked_quality = is_blocked_stage1_quality(
        result.stage1_quality_level,
        quality_reasons,
    )
    return {
        "order": order,
        "canonical_paper_key": str(row["canonical_paper_key"]),
        "source_paper_id": item.source_paper_id,
        "title": str(row.get("title") or ""),
        "source_pdf": str(source_pdf),
        "source_pdf_sha256": expected_hash,
        "cache_dir": result.cache_dir,
        "markdown_path": result.markdown_path,
        "markdown_sha256": file_sha256(result.markdown_path),
        "chunks_path": result.chunks_path,
        "chunks_sha256": file_sha256(result.chunks_path),
        "page_index_path": result.page_index_path,
        "page_index_sha256": file_sha256(result.page_index_path),
        "stage1_input_path": result.stage1_input_path,
        "stage1_input_sha256": file_sha256(result.stage1_input_path),
        "stage1_input_manifest_path": result.stage1_input_manifest_path,
        "stage1_input_manifest_sha256": file_sha256(
            result.stage1_input_manifest_path
        ),
        "stage1_quality_report_path": result.stage1_quality_report_path,
        "stage1_quality_report_sha256": file_sha256(
            result.stage1_quality_report_path
        ),
        "selected_text_source": result.selected_text_source,
        "stage1_quality_level": result.stage1_quality_level,
        "stage1_quality_reasons": quality_reasons,
        "page_count": len(result.page_index),
        "chunk_count": result.chunk_count,
        "text_length": len(result.stage1_input_text),
        "extractor_used": result.extractor_used,
        "used_ocr": result.used_ocr,
        "ocr_mode": ocr_mode,
        "blocked": blocked_quality,
    }


def _load_evidence_index(path: str | Path, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise Stage1ParentError("current-PDF evidence index is not an object")
    if payload.get("schema_version") != EVIDENCE_INDEX_SCHEMA:
        raise Stage1ParentError("current-PDF evidence index schema is not recognized")
    if payload.get("status") != "clean":
        raise Stage1ParentError(
            "current-PDF evidence index must be clean before parent materialization"
        )
    papers = payload.get("papers")
    if not isinstance(papers, list) or len(papers) != len(rows):
        raise Stage1ParentError("current-PDF evidence index does not cover the corpus")
    expected_hash_by_key = {
        str(row["canonical_paper_key"]): str(row["pdf_sha256"]) for row in rows
    }
    entries: dict[str, dict[str, Any]] = {}
    index_ocr_mode = str(payload.get("ocr_mode") or "off").strip().lower()
    for index, raw_entry in enumerate(papers):
        if not isinstance(raw_entry, Mapping):
            raise Stage1ParentError(f"evidence entry {index} is not an object")
        entry = dict(raw_entry)
        key = str(entry.get("canonical_paper_key") or "").strip()
        if not key or key in entries or key not in expected_hash_by_key:
            raise Stage1ParentError(f"evidence entry {index} has invalid identity")
        if str(entry.get("source_pdf_sha256") or "") != expected_hash_by_key[key]:
            raise Stage1ParentError(f"evidence entry PDF hash mismatch for {key}")
        entry_ocr_mode = str(entry.get("ocr_mode") or index_ocr_mode).strip().lower()
        if entry_ocr_mode not in OCR_MODES:
            raise Stage1ParentError(f"evidence entry has invalid OCR mode for {key}")
        if not _evidence_entry_valid(entry, ocr_mode=entry_ocr_mode):
            raise Stage1ParentError(f"evidence entry is not materializable for {key}")
        normalized_entry = _normalize_reused_evidence_entry(
            entry,
            ocr_mode=entry_ocr_mode,
        )
        if bool(normalized_entry.get("blocked")):
            raise Stage1ParentError(f"evidence entry is blocked for {key}")
        entries[key] = normalized_entry
    if set(entries) != set(expected_hash_by_key):
        raise Stage1ParentError("current-PDF evidence index keys do not match corpus")
    return entries


def _load_coverage_report(path: str | Path, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise Stage1ParentError("coverage report is not an object")
    if payload.get("schema_version") != COVERAGE_SCHEMA:
        raise Stage1ParentError("coverage report schema is not recognized")
    if int(payload.get("expected_corpus_count") or 0) != len(rows):
        raise Stage1ParentError("coverage report expected count does not match corpus")
    if payload.get("selected_manifest_sha256") != file_sha256(
        payload.get("selected_manifest_path") or ""
    ):
        raise Stage1ParentError("coverage report selected manifest hash is stale")
    return dict(payload)


def _summary_record_for_coverage(item: Mapping[str, Any], *, output_root: Path) -> dict[str, Any]:
    source_path = Path(str(item.get("source_path") or "")).expanduser().resolve()
    expected_hash = str(item.get("source_summary_sha256") or "")
    if not source_path.is_file() or file_sha256(source_path) != expected_hash:
        raise Stage1ParentError(
            f"registered reusable summary hash mismatch: {source_path}"
        )
    validate_registered_summary_source(
        SummarySource(
            path=str(source_path),
            source_type="workspace",
            priority=0,
            label=str(item.get("source_job_id") or source_path.parent.parent.name),
        ),
        output_root=output_root,
    )
    records = _read_summary_records(source_path)
    record_index = int(item.get("source_record_index"))
    try:
        summary = dict(records[record_index])
    except IndexError as exc:
        raise Stage1ParentError("coverage record index is outside source summary file") from exc
    expected_key = str(item.get("canonical_paper_key") or "")
    if _paper_key(summary) != expected_key:
        raise Stage1ParentError(f"coverage source record identity mismatch for {expected_key}")
    validate_canonical_ai_summary(
        summary.get("ai_summary"),
        label=f"covered Stage 1 summary {expected_key}",
    )
    return summary


def _load_generated_summary_records(paths: Sequence[str | Path]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for path_value in paths:
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise Stage1ParentError(f"generated summary file is missing: {path}")
        for index, summary in enumerate(_read_summary_records(path)):
            key = _paper_key(summary)
            if not key:
                raise Stage1ParentError(
                    f"generated summary record {index} has no canonical_paper_key: {path}"
                )
            if key in by_key:
                raise Stage1ParentError(f"generated summary identity is duplicated: {key}")
            if str(summary.get("status") or "").strip().lower() != "success":
                raise Stage1ParentError(f"generated summary is not successful: {key}")
            validate_canonical_ai_summary(
                summary.get("ai_summary"),
                label=f"generated Stage 1 summary {key}",
            )
            by_key[key] = summary
    return by_key


def _adapt_summary_for_parent(
    summary: Mapping[str, Any],
    *,
    item: PaperWorkItem,
    selected_row: Mapping[str, Any],
    evidence: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    adapted = dict(summary)
    original_info = dict(summary.get("paper_info") or {})
    paper_info = {
        **original_info,
        **dict(item.paper_info),
        "canonical_paper_key": item.canonical_paper_key,
        "source_paper_id": item.source_paper_id,
        "source_mode": item.source_mode,
        "source_pdf": item.source_pdf,
        "pdf_path": item.source_pdf,
        "source_pdf_fingerprint": str(selected_row["pdf_sha256"]),
    }
    adapted["status"] = "success"
    adapted["paper_info"] = paper_info
    adapted["preprocess"] = {
        **dict(summary.get("preprocess") or {}),
        "current_pdf_evidence_schema": EVIDENCE_INDEX_SCHEMA,
        "source_pdf_sha256": str(evidence["source_pdf_sha256"]),
        "markdown_path": str(evidence["markdown_path"]),
        "markdown_sha256": str(evidence["markdown_sha256"]),
        "chunks_path": str(evidence["chunks_path"]),
        "chunks_sha256": str(evidence["chunks_sha256"]),
        "page_index_path": str(evidence["page_index_path"]),
        "page_index_sha256": str(evidence["page_index_sha256"]),
        "stage1_input_path": str(evidence["stage1_input_path"]),
        "stage1_input_sha256": str(evidence["stage1_input_sha256"]),
        "stage1_input_manifest_path": str(evidence["stage1_input_manifest_path"]),
        "stage1_input_manifest_sha256": str(evidence["stage1_input_manifest_sha256"]),
        "stage1_quality_report_path": str(evidence["stage1_quality_report_path"]),
        "stage1_quality_report_sha256": str(evidence["stage1_quality_report_sha256"]),
        "stage1_quality_level": str(evidence.get("stage1_quality_level") or ""),
        "stage1_quality_reasons": list(evidence.get("stage1_quality_reasons") or []),
        "used_ocr": bool(evidence.get("used_ocr")),
    }
    adapted["stage1_parent_provenance"] = dict(provenance)
    return adapted


def _validate_generated_summary_binding(
    summary: Mapping[str, Any],
    *,
    item: PaperWorkItem,
    selected_row: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    key = item.canonical_paper_key
    paper_info = summary.get("paper_info")
    receipt = summary.get("stage1_generation_receipt")
    if not isinstance(paper_info, Mapping) or not isinstance(receipt, Mapping):
        raise Stage1ParentError(f"generated summary lacks its binding receipt: {key}")
    if (
        str(paper_info.get("canonical_paper_key") or "") != key
        or str(paper_info.get("source_paper_id") or "") != item.source_paper_id
        or str(paper_info.get("source_pdf_fingerprint") or "")
        != str(selected_row["pdf_sha256"])
    ):
        raise Stage1ParentError(f"generated summary identity/PDF binding failed: {key}")
    if _normalized_text(paper_info.get("title")).casefold() != _normalized_text(
        selected_row.get("title")
    ).casefold():
        raise Stage1ParentError(f"generated summary title binding failed: {key}")
    for receipt_field, evidence_field in (
        ("source_pdf_sha256", "source_pdf_sha256"),
        ("stage1_input_sha256", "stage1_input_sha256"),
        ("page_index_sha256", "page_index_sha256"),
    ):
        if str(receipt.get(receipt_field) or "") != str(
            evidence.get(evidence_field) or ""
        ):
            raise Stage1ParentError(
                f"generated summary evidence hash binding failed for {key}: {receipt_field}"
            )
    for path_field, hash_field in (
        ("request_path", "request_sha256"),
        ("raw_output_path", "raw_output_sha256"),
    ):
        path = Path(str(receipt.get(path_field) or "")).expanduser().resolve()
        if not path.is_file() or file_sha256(path) != str(receipt.get(hash_field) or ""):
            raise Stage1ParentError(
                f"generated summary receipt artifact is missing or stale for {key}: {path_field}"
            )
    anchors = receipt.get("evidence_anchors")
    if not isinstance(anchors, list) or len(anchors) < 3:
        raise Stage1ParentError(f"generated summary evidence anchors are incomplete: {key}")
    if not str(receipt.get("subagent_run_id") or "").strip():
        raise Stage1ParentError(f"generated summary subagent_run_id is missing: {key}")


def build_parent_summaries(
    *,
    selected_manifest_path: str | Path,
    coverage_report_path: str | Path,
    evidence_index_path: str | Path,
    generated_summary_files: Sequence[str | Path],
    output_root: str | Path,
    source_items: Sequence[PaperWorkItem],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _selected_rows(selected_manifest_path)
    if len(source_items) != len(rows):
        raise Stage1ParentError("runtime source bundle size does not match selected corpus")
    coverage = _load_coverage_report(coverage_report_path, rows)
    evidence_by_key = _load_evidence_index(evidence_index_path, rows)
    generated_by_key = _load_generated_summary_records(generated_summary_files)
    output = Path(output_root).expanduser().resolve()

    covered_by_key = {
        str(item.get("canonical_paper_key") or ""): dict(item)
        for item in coverage.get("covered") or []
        if isinstance(item, Mapping)
    }
    missing_keys = {
        str(item.get("canonical_paper_key") or "")
        for item in coverage.get("missing") or []
        if isinstance(item, Mapping)
    }
    if (
        int(coverage.get("covered_count") or 0) != len(covered_by_key)
        or int(coverage.get("missing_count") or 0) != len(missing_keys)
        or len(covered_by_key) + len(missing_keys) != len(rows)
    ):
        raise Stage1ParentError("coverage report does not partition covered/missing keys")
    if set(generated_by_key) != missing_keys:
        extra = sorted(set(generated_by_key) - missing_keys)
        absent = sorted(missing_keys - set(generated_by_key))
        raise Stage1ParentError(
            "generated summaries must exactly cover coverage-report missing keys; "
            f"extra={extra[:5]}, missing={absent[:5]}"
        )

    source_items_by_key = {item.canonical_paper_key: item for item in source_items}
    summaries: list[dict[str, Any]] = []
    provenance_items: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for order, row in enumerate(rows, start=1):
        key = str(row["canonical_paper_key"])
        item = source_items_by_key.get(key)
        if item is None:
            raise Stage1ParentError(f"runtime source bundle is missing {key}")
        if str(row["pdf_sha256"]) != file_sha256(item.source_pdf):
            raise Stage1ParentError(f"runtime source PDF hash mismatch for {key}")
        if key in covered_by_key:
            coverage_item = covered_by_key[key]
            source_summary = _summary_record_for_coverage(
                coverage_item,
                output_root=output,
            )
            provenance = {
                "source_kind": "registry_reuse",
                "coverage_report_path": str(Path(coverage_report_path).resolve()),
                "source_path": coverage_item["source_path"],
                "source_summary_sha256": coverage_item["source_summary_sha256"],
                "source_record_index": coverage_item["source_record_index"],
                "source_job_id": coverage_item["source_job_id"],
                "source_artifact_id": coverage_item["source_artifact_id"],
                "model_call_count": 0,
            }
        else:
            source_summary = generated_by_key[key]
            _validate_generated_summary_binding(
                source_summary,
                item=item,
                selected_row=row,
                evidence=evidence_by_key[key],
            )
            provenance = {
                "source_kind": "external_generated_summary",
                "coverage_report_path": str(Path(coverage_report_path).resolve()),
                "model_call_count": 1,
            }
        adapted = _adapt_summary_for_parent(
            source_summary,
            item=item,
            selected_row=row,
            evidence=evidence_by_key[key],
            provenance={**provenance, "order": order},
        )
        summaries.append(adapted)
        provenance_items.append(
            {
                "canonical_paper_key": key,
                "title": str(row.get("title") or ""),
                "pdf_sha256": str(row["pdf_sha256"]),
                **provenance,
            }
        )
    return summaries, provenance_items, rejected


def prepare_stage1_subagent_requests(
    *,
    bundle_dir: str | Path,
    work_dir: str | Path,
) -> dict[str, Any]:
    """Emit one hash-bound, resumable Codex-native request per missing paper."""

    bundle = Path(bundle_dir).expanduser().resolve()
    work = Path(work_dir).expanduser().resolve()
    audit_bundle(bundle)
    selected_manifest = bundle / SELECTED_MANIFEST_NAME
    rows = _selected_rows(selected_manifest)
    coverage_path = work / COVERAGE_REPORT_NAME
    evidence_path = work / EVIDENCE_INDEX_NAME
    coverage = _load_coverage_report(coverage_path, rows)
    evidence_by_key = _load_evidence_index(evidence_path, rows)
    missing_keys = {
        str(item.get("canonical_paper_key") or "")
        for item in coverage.get("missing") or []
        if isinstance(item, Mapping)
    }
    if len(missing_keys) != int(coverage.get("missing_count") or 0):
        raise Stage1ParentError("coverage report missing identities are inconsistent")

    spec = load_runtime_job_spec(bundle / PARENT_SPEC_NAME)
    source_bundle = AgentRuntimeBridge(spec).build_source_bundle()
    source_items = {
        item.canonical_paper_key: item for item in source_bundle.paper_work_items
    }
    if len(source_items) != EXPECTED_CORPUS_COUNT:
        raise Stage1ParentError("runtime source bundle does not cover the frozen corpus")

    request_dir = work / "stage1_subagent_requests"
    raw_dir = work / "stage1_subagent_raw"
    generated_dir = work / "stage1_generated_summaries"
    request_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)

    requests: list[dict[str, Any]] = []
    for order, row in enumerate(rows, start=1):
        key = str(row["canonical_paper_key"])
        if key not in missing_keys:
            continue
        item = source_items[key]
        evidence = evidence_by_key[key]
        raw_authors = row.get("authors") or item.paper_info.get("authors") or []
        authors = (
            [str(author) for author in raw_authors]
            if isinstance(raw_authors, (list, tuple))
            else [str(raw_authors)]
        )
        request_path = request_dir / f"request_{order:03d}.json"
        raw_output_path = raw_dir / f"raw_{order:03d}.json"
        final_output_path = generated_dir / f"summary_{order:03d}.json"
        request = {
            "artifact_type": "stage1_subagent_request",
            "artifact_version": "v1",
            "schema_version": GENERATION_REQUEST_SCHEMA,
            "order": order,
            "canonical_paper_key": key,
            "paper_info": {
                **dict(item.paper_info),
                "title": str(row.get("title") or item.paper_info.get("title") or ""),
                "authors": authors,
                "year": str(row.get("year") or item.paper_info.get("year") or ""),
                "journal": str(row.get("journal") or item.paper_info.get("journal") or ""),
                "doi": str(row.get("doi") or item.paper_info.get("doi") or ""),
                "canonical_paper_key": key,
                "source_paper_id": item.source_paper_id,
                "source_pdf": item.source_pdf,
                "source_pdf_fingerprint": str(row["pdf_sha256"]),
            },
            "evidence": {
                field: evidence[field]
                for field in (
                    "source_pdf",
                    "source_pdf_sha256",
                    "stage1_input_path",
                    "stage1_input_sha256",
                    "page_index_path",
                    "page_index_sha256",
                    "chunks_path",
                    "chunks_sha256",
                    "stage1_quality_report_path",
                    "stage1_quality_report_sha256",
                    "stage1_quality_level",
                    "stage1_quality_reasons",
                    "ocr_mode",
                    "used_ocr",
                )
            },
            "raw_output_path": str(raw_output_path),
            "final_output_path": str(final_output_path),
            "task_instructions": [
                "Read the complete stage1_input and page_index artifacts before writing.",
                "Use only claims supported by this paper; do not import facts from other papers.",
                "Write raw_output_path as JSON with ai_summary, evidence_anchors, and subagent_run_id.",
                "Cover routing, core_analysis, paper_metadata, and the active specialized_details branch.",
                "Use the exact title, authors, year, journal, and DOI from paper_info.",
                "Provide at least three verbatim evidence anchors from at least two pages.",
                "Each anchor must contain page_number, quote, and supports_fields.",
                "Do not write the final output path; local validation materializes it.",
            ],
        }
        _atomic_write_json_with_retry(request_path, request)
        requests.append(
            {
                "order": order,
                "canonical_paper_key": key,
                "request_path": str(request_path),
                "request_sha256": file_sha256(request_path),
                "raw_output_path": str(raw_output_path),
                "final_output_path": str(final_output_path),
            }
        )

    if len(requests) != len(missing_keys):
        raise Stage1ParentError("subagent requests do not cover every missing identity")
    manifest = {
        "artifact_type": "stage1_subagent_request_manifest",
        "artifact_version": "v1",
        "schema_version": GENERATION_MANIFEST_SCHEMA,
        "created_at": utc_now_iso(),
        "status": "ready",
        "provider_executed": False,
        "selected_manifest_path": str(selected_manifest),
        "selected_manifest_sha256": file_sha256(selected_manifest),
        "coverage_report_path": str(coverage_path),
        "coverage_report_sha256": file_sha256(coverage_path),
        "evidence_index_path": str(evidence_path),
        "evidence_index_sha256": file_sha256(evidence_path),
        "expected_request_count": len(missing_keys),
        "request_count": len(requests),
        "requests": requests,
    }
    manifest_path = work / GENERATION_REQUEST_MANIFEST_NAME
    _atomic_write_json_with_retry(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def finalize_stage1_subagent_summary(
    *,
    request_path: str | Path,
    raw_output_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate one subagent analysis against exact local evidence and persist it."""

    request_target = Path(request_path).expanduser().resolve()
    raw_target = Path(raw_output_path).expanduser().resolve()
    request = _read_json(request_target)
    raw = _read_json(raw_target)
    if not isinstance(request, Mapping) or request.get("schema_version") != GENERATION_REQUEST_SCHEMA:
        raise Stage1ParentError("Stage 1 subagent request schema is not recognized")
    if not isinstance(raw, Mapping):
        raise Stage1ParentError("Stage 1 subagent output is not an object")
    ai_payload = raw.get("ai_summary")
    if not isinstance(ai_payload, Mapping):
        raise Stage1ParentError("Stage 1 subagent output has no ai_summary object")
    canonical_ai_summary = normalize_ai_summary(ai_payload)
    validate_canonical_ai_summary(
        canonical_ai_summary,
        label=f"generated Stage 1 summary {request['canonical_paper_key']}",
    )

    request_info = dict(request.get("paper_info") or {})
    metadata = dict(canonical_ai_summary.get("paper_metadata") or {})
    if _normalized_text(metadata.get("title")).casefold() != _normalized_text(
        request_info.get("title")
    ).casefold():
        raise Stage1ParentError("generated summary title does not match its request")
    expected_doi = _normalized_text(request_info.get("doi")).casefold()
    if expected_doi and _normalized_text(metadata.get("doi")).casefold() != expected_doi:
        raise Stage1ParentError("generated summary DOI does not match its request")
    quality = dict(canonical_ai_summary.get("quality_audit") or {})
    if (
        bool(quality.get("needs_manual_review"))
        or list(quality.get("missing_critical_fields") or [])
        or float(quality.get("completeness_score") or 0.0) < 0.9
    ):
        raise Stage1ParentError(
            "generated summary does not meet the canonical completeness gate"
        )

    evidence = dict(request.get("evidence") or {})
    page_index_path = Path(str(evidence.get("page_index_path") or "")).resolve()
    if (
        not page_index_path.is_file()
        or file_sha256(page_index_path) != evidence.get("page_index_sha256")
    ):
        raise Stage1ParentError("request page-index evidence is missing or stale")
    page_index = _read_json(page_index_path)
    if not isinstance(page_index, list):
        raise Stage1ParentError("request page-index evidence is not an array")
    page_text = {
        int(item.get("page_number")): _normalized_text(item.get("text"))
        for item in page_index
        if isinstance(item, Mapping) and item.get("page_number") is not None
    }
    anchors = raw.get("evidence_anchors")
    if not isinstance(anchors, list) or len(anchors) < 3:
        raise Stage1ParentError("generated summary requires at least three evidence anchors")
    normalized_anchors: list[dict[str, Any]] = []
    anchored_pages: set[int] = set()
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, Mapping):
            raise Stage1ParentError(f"evidence anchor {index} is not an object")
        try:
            page_number = int(anchor.get("page_number"))
        except (TypeError, ValueError) as exc:
            raise Stage1ParentError(
                f"evidence anchor {index} has no valid page number"
            ) from exc
        quote = _normalized_text(anchor.get("quote"))
        supports_fields = [
            str(field) for field in anchor.get("supports_fields") or [] if str(field)
        ]
        if (
            len(quote) < 20
            or page_number not in page_text
            or quote.casefold() not in page_text[page_number].casefold()
            or not supports_fields
        ):
            raise Stage1ParentError(
                f"evidence anchor {index} is not a verbatim page-index excerpt"
            )
        anchored_pages.add(page_number)
        normalized_anchors.append(
            {
                "page_number": page_number,
                "quote": quote,
                "supports_fields": supports_fields,
            }
        )
    if len(anchored_pages) < 2 and len(page_text) > 1:
        raise Stage1ParentError("evidence anchors must span at least two pages")

    source_pdf = Path(str(evidence.get("source_pdf") or "")).resolve()
    stage1_input = Path(str(evidence.get("stage1_input_path") or "")).resolve()
    for path, expected_hash, label in (
        (source_pdf, evidence.get("source_pdf_sha256"), "source PDF"),
        (stage1_input, evidence.get("stage1_input_sha256"), "Stage 1 input"),
    ):
        if not path.is_file() or file_sha256(path) != expected_hash:
            raise Stage1ParentError(f"request {label} is missing or stale")
    subagent_run_id = str(raw.get("subagent_run_id") or "").strip()
    if not subagent_run_id:
        raise Stage1ParentError("generated summary has no subagent_run_id")

    record = {
        "paper_info": request_info,
        "status": "success",
        "ai_summary": canonical_ai_summary,
        "source_mode": "codex_native_subagent",
        "model_used": "codex_native_subagent",
        "stage1_generation_receipt": {
            "schema_version": GENERATION_REQUEST_SCHEMA,
            "request_path": str(request_target),
            "request_sha256": file_sha256(request_target),
            "raw_output_path": str(raw_target),
            "raw_output_sha256": file_sha256(raw_target),
            "subagent_run_id": subagent_run_id,
            "source_pdf_sha256": str(evidence["source_pdf_sha256"]),
            "stage1_input_sha256": str(evidence["stage1_input_sha256"]),
            "page_index_sha256": str(evidence["page_index_sha256"]),
            "evidence_anchors": normalized_anchors,
        },
    }
    output_value = output_path or request.get("final_output_path")
    if not output_value:
        raise Stage1ParentError("generated summary output path is empty")
    final_target = Path(output_value).expanduser().resolve()
    final_target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json_with_retry(final_target, record)
    return {
        "status": "clean",
        "canonical_paper_key": request["canonical_paper_key"],
        "output_path": str(final_target),
        "output_sha256": file_sha256(final_target),
        "subagent_run_id": subagent_run_id,
        "evidence_anchor_count": len(normalized_anchors),
    }


def materialize_parent(
    *,
    bundle_dir: str | Path,
    work_dir: str | Path,
    generated_summary_files: Sequence[str | Path],
    job_id: str = "",
    legacy_main: Any | None = None,
) -> dict[str, Any]:
    """Materialize the 84-paper Stage 1 parent from verified local inputs only."""

    bundle = Path(bundle_dir).expanduser().resolve()
    work = Path(work_dir).expanduser().resolve()
    audit_bundle(bundle)
    coverage_report = work / COVERAGE_REPORT_NAME
    evidence_index = work / EVIDENCE_INDEX_NAME
    spec = load_runtime_job_spec(bundle / PARENT_SPEC_NAME)
    if job_id:
        from dataclasses import replace

        spec = replace(spec, job_id=job_id)
    selected_manifest = bundle / SELECTED_MANIFEST_NAME
    coverage = _load_coverage_report(coverage_report, _selected_rows(selected_manifest))
    output_root = Path(str(coverage.get("output_root") or "")).expanduser().resolve()

    def stage_handler(stage_name: str, request: Any) -> dict[str, Any]:
        if stage_name != "stage1_analyze":
            raise Stage1ParentError(f"unexpected runtime stage: {stage_name}")
        summaries, source_items, rejected = build_parent_summaries(
            selected_manifest_path=selected_manifest,
            coverage_report_path=coverage_report,
            evidence_index_path=evidence_index,
            generated_summary_files=generated_summary_files,
            output_root=output_root,
            source_items=request.source_bundle.paper_work_items,
        )
        manifest_sources = [
            {
                "path": str(coverage_report),
                "source_type": "coverage_audit",
                "label": "stage1_registered_coverage_audit",
                "priority": 0,
                "content_hash": file_sha256(coverage_report),
            },
            {
                "path": str(evidence_index),
                "source_type": "current_pdf_evidence",
                "label": "stage1_current_pdf_evidence_index",
                "priority": 1,
                "content_hash": file_sha256(evidence_index),
            },
            *[
                {
                    "path": str(Path(path).expanduser().resolve()),
                    "source_type": "external_generated_summary_file",
                    "label": f"generated_summary_file:{index}",
                    "priority": 2 + index,
                    "content_hash": file_sha256(path),
                }
                for index, path in enumerate(generated_summary_files)
            ],
            *source_items,
        ]
        return {
            "summaries": summaries,
            "source_items": manifest_sources,
            "rejected_candidates": rejected,
            "model_call_count": int(coverage.get("missing_count") or 0),
            "subagent_run_id": "external-generated-stage1-parent-import",
        }

    if legacy_main is None:
        import main as legacy_main_module

        legacy_main = legacy_main_module
    result = AgentRuntimeRunner(
        spec,
        legacy_main=legacy_main,
        stage_handler=stage_handler,
    ).run()
    reconcile = AgentRuntimeRunner.reconcile(result.workspace_path)
    report = {
        "artifact_type": "stage1_parent_materialization_report",
        "artifact_version": "v1",
        "schema_version": MATERIALIZATION_SCHEMA,
        "created_at": utc_now_iso(),
        "provider_executed": False,
        "bundle_dir": str(bundle),
        "coverage_report_path": str(coverage_report),
        "coverage_report_sha256": file_sha256(coverage_report),
        "evidence_index_path": str(evidence_index),
        "evidence_index_sha256": file_sha256(evidence_index),
        "generated_summary_files": [
            {
                "path": str(Path(path).expanduser().resolve()),
                "sha256": file_sha256(path),
            }
            for path in generated_summary_files
        ],
        "expected_corpus_count": EXPECTED_CORPUS_COUNT,
        "reused_summary_count": int(coverage.get("covered_count") or 0),
        "generated_summary_count": int(coverage.get("missing_count") or 0),
        "model_call_count": int(coverage.get("missing_count") or 0),
        "runtime": {
            "job_id": result.job_id,
            "workspace_path": result.workspace_path,
            "job_status": result.job_status,
            "job_disposition": result.job_disposition,
            "canonical_ready": result.canonical_ready,
            "requires_attention": result.requires_attention,
            "completed_stages": list(result.completed_stages),
            "job_outcome_path": result.job_outcome_path,
        },
        "reconcile": {
            "clean": reconcile.clean,
            "completed_stages": list(reconcile.completed_stages),
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "artifact_id": issue.artifact_id,
                    "stage_name": issue.stage_name,
                }
                for issue in reconcile.issues
            ],
            "repaired_artifact_ids": list(reconcile.repaired_artifact_ids),
            "reconstructed_stage_records": list(reconcile.reconstructed_stage_records),
            "outcome_repaired": reconcile.outcome_repaired,
            "pointer_repaired": reconcile.pointer_repaired,
        },
    }
    report_path = Path(result.workspace_path) / "artifacts" / MATERIALIZATION_REPORT_NAME
    report["materialization_report_path"] = str(report_path)
    atomic_write_json(str(report_path), report)
    registry = ArtifactRegistry(
        Path(result.workspace_path) / "artifact_registry.json",
        result.job_id,
    )
    registry.register_file(
        artifact_role="materialization_report",
        artifact_type="stage1_parent_materialization_report",
        artifact_version="v1",
        path=report_path,
        producer="scripts.pph_stage1_parent.materialize_parent",
        artifact_id="stage1_parent_materialization_report",
    )
    report["materialization_report_sha256"] = file_sha256(report_path)
    return report


def prepare_current_pdf_evidence(
    *,
    bundle_dir: str | Path,
    work_dir: str | Path,
    ocr_mode: str = "off",
) -> dict[str, Any]:
    """Prepare or resume local full-text evidence for all 84 frozen PDFs."""

    normalized_ocr_mode = str(ocr_mode or "").strip().lower()
    if normalized_ocr_mode not in OCR_MODES:
        raise Stage1ParentError(
            f"ocr_mode must be one of {sorted(OCR_MODES)}, got {ocr_mode!r}"
        )
    work = Path(work_dir).expanduser().resolve()
    with _exclusive_work_lock(work):
        return _prepare_current_pdf_evidence_unlocked(
            bundle_dir=bundle_dir,
            work_dir=work,
            ocr_mode=normalized_ocr_mode,
        )


def _prepare_current_pdf_evidence_unlocked(
    *,
    bundle_dir: str | Path,
    work_dir: str | Path,
    ocr_mode: str,
) -> dict[str, Any]:
    bundle = Path(bundle_dir).expanduser().resolve()
    work = Path(work_dir).expanduser().resolve()
    audit_bundle(bundle)
    rows = _selected_rows(bundle / SELECTED_MANIFEST_NAME)
    spec = load_runtime_job_spec(bundle / PARENT_SPEC_NAME)
    source_bundle = AgentRuntimeBridge(spec).build_source_bundle()
    if len(source_bundle.paper_work_items) != EXPECTED_CORPUS_COUNT:
        raise Stage1ParentError(
            "runtime source bundle does not contain the frozen 84-paper corpus"
        )
    items = {
        item.canonical_paper_key: item for item in source_bundle.paper_work_items
    }
    if len(items) != EXPECTED_CORPUS_COUNT:
        raise Stage1ParentError("runtime source bundle contains duplicate identities")

    work.mkdir(parents=True, exist_ok=True)
    index_path = work / EVIDENCE_INDEX_NAME
    existing_payload: Mapping[str, Any] = {}
    if index_path.is_file():
        raw_existing = _read_json(index_path)
        if isinstance(raw_existing, Mapping):
            existing_payload = raw_existing
    existing_entries = {
        str(item.get("canonical_paper_key") or ""): dict(item)
        for item in (existing_payload.get("papers") or [])
        if isinstance(item, Mapping)
    }

    tesseract_path = _ensure_tesseract_on_path()
    cache_root = (
        work / "preprocess_cache"
        if ocr_mode == "off"
        else work / f"preprocess_cache_ocr_{ocr_mode}"
    )
    manager = PreprocessManager(_local_preprocess_config(cache_root, ocr_mode=ocr_mode))
    prepared: list[dict[str, Any]] = []
    reused_count = 0
    blocked: list[str] = []
    for order, row in enumerate(rows, start=1):
        key = str(row["canonical_paper_key"])
        print(
            json.dumps(
                {
                    "event": "preprocess_start",
                    "order": order,
                    "total": EXPECTED_CORPUS_COUNT,
                    "canonical_paper_key": key,
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
        item = items.get(key)
        if item is None:
            raise Stage1ParentError(f"runtime source bundle is missing {key}")
        source_pdf = Path(item.source_pdf).expanduser().resolve()
        expected_hash = str(row["pdf_sha256"])
        if not source_pdf.is_file() or file_sha256(source_pdf) != expected_hash:
            raise Stage1ParentError(f"frozen source PDF hash mismatch for {key}")

        existing = existing_entries.get(key)
        reused = bool(
            existing
            and str(existing.get("source_pdf_sha256") or "") == expected_hash
            and _evidence_entry_valid(existing, ocr_mode=ocr_mode)
        )
        if reused:
            entry = _normalize_reused_evidence_entry(
                existing,
                ocr_mode=ocr_mode,
            )
            reused_count += 1
        else:
            result = manager.prepare_pdf(str(source_pdf))
            if result is None:
                raise Stage1ParentError(f"local preprocess returned no result for {key}")
            entry = _evidence_entry_from_result(
                result=result,
                order=order,
                row=row,
                item=item,
                source_pdf=source_pdf,
                expected_hash=expected_hash,
                ocr_mode=ocr_mode,
            )
        if bool(entry.get("blocked")):
            blocked.append(key)
        prepared.append(entry)
        print(
            json.dumps(
                {
                    "event": "preprocess_complete",
                    "order": order,
                    "total": EXPECTED_CORPUS_COUNT,
                    "canonical_paper_key": key,
                    "stage1_quality_level": entry.get("stage1_quality_level"),
                    "blocked": bool(entry.get("blocked")),
                    "reused": reused,
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
        partial_payload = {
            "artifact_type": "stage1_current_pdf_evidence_index",
            "artifact_version": "v1",
            "schema_version": EVIDENCE_INDEX_SCHEMA,
            "created_at": utc_now_iso(),
            "status": "running",
            "provider_executed": False,
            "ocr_mode": ocr_mode,
            "tesseract_path": tesseract_path,
            "bundle_dir": str(bundle),
            "selected_manifest_path": str(bundle / SELECTED_MANIFEST_NAME),
            "selected_manifest_sha256": file_sha256(
                bundle / SELECTED_MANIFEST_NAME
            ),
            "expected_count": EXPECTED_CORPUS_COUNT,
            "prepared_count": len(prepared),
            "reused_count": reused_count,
            "blocked_count": len(blocked),
            "blocked_keys": blocked,
            "papers": prepared,
        }
        _atomic_write_json_with_retry(index_path, partial_payload)

    status = "clean" if not blocked else "findings"
    final_payload = {
        **partial_payload,
        "created_at": utc_now_iso(),
        "status": status,
        "prepared_count": len(prepared),
        "blocked_count": len(blocked),
        "blocked_keys": blocked,
    }
    _atomic_write_json_with_retry(index_path, final_payload)
    return final_payload


def repair_blocked_pdf_evidence(
    *,
    bundle_dir: str | Path,
    work_dir: str | Path,
) -> dict[str, Any]:
    """Reprocess only blocked evidence entries with forced local OCR."""

    bundle = Path(bundle_dir).expanduser().resolve()
    work = Path(work_dir).expanduser().resolve()
    with _exclusive_work_lock(work):
        audit_bundle(bundle)
        rows = _selected_rows(bundle / SELECTED_MANIFEST_NAME)
        index_path = work / EVIDENCE_INDEX_NAME
        payload = _read_json(index_path)
        if not isinstance(payload, Mapping):
            raise Stage1ParentError("current-PDF evidence index is not an object")
        if payload.get("schema_version") != EVIDENCE_INDEX_SCHEMA:
            raise Stage1ParentError("current-PDF evidence index schema is not recognized")
        if payload.get("selected_manifest_sha256") != file_sha256(
            bundle / SELECTED_MANIFEST_NAME
        ):
            raise Stage1ParentError("current-PDF evidence index is bound to another corpus")
        raw_papers = payload.get("papers")
        if (
            not isinstance(raw_papers, list)
            or len(raw_papers) != EXPECTED_CORPUS_COUNT
            or int(payload.get("prepared_count") or 0) != EXPECTED_CORPUS_COUNT
        ):
            raise Stage1ParentError(
                "targeted OCR requires a complete 84-paper baseline evidence index"
            )

        spec = load_runtime_job_spec(bundle / PARENT_SPEC_NAME)
        source_bundle = AgentRuntimeBridge(spec).build_source_bundle()
        items = {
            item.canonical_paper_key: item for item in source_bundle.paper_work_items
        }
        if len(items) != EXPECTED_CORPUS_COUNT:
            raise Stage1ParentError("runtime source bundle does not cover the frozen corpus")

        tesseract_path = _ensure_tesseract_on_path()
        if not tesseract_path:
            raise Stage1ParentError("targeted OCR requires a local Tesseract executable")
        manager = PreprocessManager(
            _local_preprocess_config(
                work / "preprocess_cache_ocr_always",
                ocr_mode="always",
            )
        )

        repaired: list[dict[str, Any]] = []
        repaired_keys = [
            str(key)
            for key in payload.get("targeted_ocr_repaired_keys", [])
            if str(key)
        ]
        remaining_blocked: list[str] = []
        for order, (row, raw_entry) in enumerate(zip(rows, raw_papers), start=1):
            if not isinstance(raw_entry, Mapping):
                raise Stage1ParentError(f"evidence entry {order} is not an object")
            key = str(row["canonical_paper_key"])
            entry = dict(raw_entry)
            if str(entry.get("canonical_paper_key") or "") != key:
                raise Stage1ParentError(f"evidence order/identity mismatch for {key}")
            entry_mode = str(
                entry.get("ocr_mode") or payload.get("ocr_mode") or "off"
            ).strip().lower()
            if entry_mode not in OCR_MODES or not _evidence_entry_valid(
                entry,
                ocr_mode=entry_mode,
            ):
                raise Stage1ParentError(f"baseline evidence integrity failed for {key}")
            entry = _normalize_reused_evidence_entry(entry, ocr_mode=entry_mode)

            if bool(entry.get("blocked")):
                item = items.get(key)
                if item is None:
                    raise Stage1ParentError(f"runtime source bundle is missing {key}")
                source_pdf = Path(item.source_pdf).expanduser().resolve()
                expected_hash = str(row["pdf_sha256"])
                if not source_pdf.is_file() or file_sha256(source_pdf) != expected_hash:
                    raise Stage1ParentError(f"frozen source PDF hash mismatch for {key}")
                print(
                    json.dumps(
                        {
                            "event": "targeted_ocr_start",
                            "order": order,
                            "canonical_paper_key": key,
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                result = manager.prepare_pdf(str(source_pdf))
                if result is None:
                    raise Stage1ParentError(
                        f"targeted OCR returned no result for {key}"
                    )
                entry = _evidence_entry_from_result(
                    result=result,
                    order=order,
                    row=row,
                    item=item,
                    source_pdf=source_pdf,
                    expected_hash=expected_hash,
                    ocr_mode="always",
                )
                if key not in repaired_keys:
                    repaired_keys.append(key)
                print(
                    json.dumps(
                        {
                            "event": "targeted_ocr_complete",
                            "order": order,
                            "canonical_paper_key": key,
                            "stage1_quality_level": entry["stage1_quality_level"],
                            "blocked": bool(entry["blocked"]),
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )

            if bool(entry.get("blocked")):
                remaining_blocked.append(key)
            repaired.append(entry)
            combined_papers = [
                *repaired,
                *[
                    dict(item)
                    for item in raw_papers[len(repaired) :]
                    if isinstance(item, Mapping)
                ],
            ]
            combined_blocked = [
                str(item.get("canonical_paper_key") or "")
                for item in combined_papers
                if bool(item.get("blocked"))
            ]
            partial_payload = {
                **dict(payload),
                "created_at": utc_now_iso(),
                "status": "running",
                "ocr_mode": "mixed",
                "tesseract_path": tesseract_path,
                "prepared_count": EXPECTED_CORPUS_COUNT,
                "blocked_count": len(combined_blocked),
                "blocked_keys": combined_blocked,
                "targeted_ocr_repaired_keys": repaired_keys,
                "papers": combined_papers,
            }
            _atomic_write_json_with_retry(index_path, partial_payload)

        final_payload = {
            **partial_payload,
            "created_at": utc_now_iso(),
            "status": "clean" if not remaining_blocked else "findings",
            "prepared_count": len(repaired),
            "blocked_count": len(remaining_blocked),
            "blocked_keys": remaining_blocked,
            "targeted_ocr_repaired_count": len(repaired_keys),
            "targeted_ocr_repaired_keys": repaired_keys,
            "papers": repaired,
        }
        _atomic_write_json_with_retry(index_path, final_payload)
        return final_payload


def prewarm_one_ocr_cache(
    *,
    bundle_dir: str | Path,
    work_dir: str | Path,
    canonical_paper_key: str,
) -> dict[str, Any]:
    """Populate one forced-OCR cache entry without mutating the evidence index."""

    bundle = Path(bundle_dir).expanduser().resolve()
    work = Path(work_dir).expanduser().resolve()
    audit_bundle(bundle)
    rows = _selected_rows(bundle / SELECTED_MANIFEST_NAME)
    row_by_key = {str(row["canonical_paper_key"]): row for row in rows}
    row = row_by_key.get(canonical_paper_key)
    if row is None:
        raise Stage1ParentError(
            f"prewarm paper is not in the frozen corpus: {canonical_paper_key}"
        )
    spec = load_runtime_job_spec(bundle / PARENT_SPEC_NAME)
    source_bundle = AgentRuntimeBridge(spec).build_source_bundle()
    item_by_key = {
        item.canonical_paper_key: item for item in source_bundle.paper_work_items
    }
    item = item_by_key.get(canonical_paper_key)
    if item is None:
        raise Stage1ParentError(
            f"runtime source bundle is missing {canonical_paper_key}"
        )
    source_pdf = Path(item.source_pdf).expanduser().resolve()
    expected_hash = str(row["pdf_sha256"])
    if not source_pdf.is_file() or file_sha256(source_pdf) != expected_hash:
        raise Stage1ParentError(
            f"frozen source PDF hash mismatch for {canonical_paper_key}"
        )
    if not _ensure_tesseract_on_path():
        raise Stage1ParentError("OCR prewarm requires a local Tesseract executable")
    manager = PreprocessManager(
        _local_preprocess_config(
            work / "preprocess_cache_ocr_always",
            ocr_mode="always",
        )
    )
    result = manager.prepare_pdf(str(source_pdf))
    if result is None:
        raise Stage1ParentError(
            f"OCR prewarm returned no result for {canonical_paper_key}"
        )
    order = next(
        index
        for index, candidate in enumerate(rows, start=1)
        if str(candidate["canonical_paper_key"]) == canonical_paper_key
    )
    entry = _evidence_entry_from_result(
        result=result,
        order=order,
        row=row,
        item=item,
        source_pdf=source_pdf,
        expected_hash=expected_hash,
        ocr_mode="always",
    )
    return {
        "status": "clean" if not entry["blocked"] else "findings",
        "order": order,
        "canonical_paper_key": canonical_paper_key,
        "cache_dir": entry["cache_dir"],
        "stage1_quality_level": entry["stage1_quality_level"],
        "stage1_quality_reasons": entry["stage1_quality_reasons"],
        "blocked": entry["blocked"],
        "used_ocr": entry["used_ocr"],
        "stage1_input_sha256": entry["stage1_input_sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit reuse and prepare local evidence for the 84-paper PPH parent."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit-coverage")
    audit.add_argument("--bundle-dir", type=Path, required=True)
    audit.add_argument("--output-root", type=Path, required=True)
    audit.add_argument("--work-dir", type=Path, required=True)

    prepare = subparsers.add_parser("prepare-evidence")
    prepare.add_argument("--bundle-dir", type=Path, required=True)
    prepare.add_argument("--work-dir", type=Path, required=True)
    prepare.add_argument("--ocr-mode", choices=sorted(OCR_MODES), default="off")

    repair = subparsers.add_parser("repair-blocked-evidence")
    repair.add_argument("--bundle-dir", type=Path, required=True)
    repair.add_argument("--work-dir", type=Path, required=True)

    prewarm = subparsers.add_parser("prewarm-evidence-ocr")
    prewarm.add_argument("--bundle-dir", type=Path, required=True)
    prewarm.add_argument("--work-dir", type=Path, required=True)
    prewarm.add_argument("--paper-key", required=True)

    requests = subparsers.add_parser("prepare-generation-requests")
    requests.add_argument("--bundle-dir", type=Path, required=True)
    requests.add_argument("--work-dir", type=Path, required=True)

    finalize = subparsers.add_parser("finalize-generated-summary")
    finalize.add_argument("--request", type=Path, required=True)
    finalize.add_argument("--raw-output", type=Path, required=True)
    finalize.add_argument("--output", type=Path)

    materialize = subparsers.add_parser("materialize-parent")
    materialize.add_argument("--bundle-dir", type=Path, required=True)
    materialize.add_argument("--work-dir", type=Path, required=True)
    materialize.add_argument(
        "--generated-summary-file",
        type=Path,
        action="append",
        default=[],
        help="JSON array or object with summaries for the coverage-report missing keys.",
    )
    materialize.add_argument("--job-id", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "audit-coverage":
        audit_bundle(args.bundle_dir)
        payload = audit_registered_summary_coverage(
            selected_manifest_path=args.bundle_dir / SELECTED_MANIFEST_NAME,
            output_root=args.output_root,
        )
        args.work_dir.mkdir(parents=True, exist_ok=True)
        output_path = args.work_dir / COVERAGE_REPORT_NAME
        atomic_write_json(str(output_path), payload)
        print(
            json.dumps(
                {
                    "status": payload["status"],
                    "covered_count": payload["covered_count"],
                    "missing_count": payload["missing_count"],
                    "ambiguous_count": payload["ambiguous_count"],
                    "invalid_candidate_count": payload["invalid_candidate_count"],
                    "report_path": str(output_path.resolve()),
                    "report_sha256": file_sha256(output_path),
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0
    if args.command == "prepare-evidence":
        payload = prepare_current_pdf_evidence(
            bundle_dir=args.bundle_dir,
            work_dir=args.work_dir,
            ocr_mode=args.ocr_mode,
        )
        print(
            json.dumps(
                {
                    "status": payload["status"],
                    "prepared_count": payload["prepared_count"],
                    "reused_count": payload["reused_count"],
                    "blocked_count": payload["blocked_count"],
                    "index_path": str(
                        (args.work_dir / EVIDENCE_INDEX_NAME).resolve()
                    ),
                    "index_sha256": file_sha256(
                        args.work_dir / EVIDENCE_INDEX_NAME
                    ),
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0 if payload["status"] == "clean" else 2
    if args.command == "repair-blocked-evidence":
        payload = repair_blocked_pdf_evidence(
            bundle_dir=args.bundle_dir,
            work_dir=args.work_dir,
        )
        print(
            json.dumps(
                {
                    "status": payload["status"],
                    "prepared_count": payload["prepared_count"],
                    "blocked_count": payload["blocked_count"],
                    "targeted_ocr_repaired_count": payload[
                        "targeted_ocr_repaired_count"
                    ],
                    "index_path": str(
                        (args.work_dir / EVIDENCE_INDEX_NAME).resolve()
                    ),
                    "index_sha256": file_sha256(
                        args.work_dir / EVIDENCE_INDEX_NAME
                    ),
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0 if payload["status"] == "clean" else 2
    if args.command == "prewarm-evidence-ocr":
        payload = prewarm_one_ocr_cache(
            bundle_dir=args.bundle_dir,
            work_dir=args.work_dir,
            canonical_paper_key=args.paper_key,
        )
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload["status"] == "clean" else 2
    if args.command == "prepare-generation-requests":
        payload = prepare_stage1_subagent_requests(
            bundle_dir=args.bundle_dir,
            work_dir=args.work_dir,
        )
        print(
            json.dumps(
                {
                    "status": payload["status"],
                    "request_count": payload["request_count"],
                    "manifest_path": payload["manifest_path"],
                    "manifest_sha256": file_sha256(payload["manifest_path"]),
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0
    if args.command == "finalize-generated-summary":
        payload = finalize_stage1_subagent_summary(
            request_path=args.request,
            raw_output_path=args.raw_output,
            output_path=args.output,
        )
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0
    if args.command == "materialize-parent":
        payload = materialize_parent(
            bundle_dir=args.bundle_dir,
            work_dir=args.work_dir,
            generated_summary_files=args.generated_summary_file,
            job_id=args.job_id,
        )
        print(
            json.dumps(
                {
                    "job_id": payload["runtime"]["job_id"],
                    "workspace_path": payload["runtime"]["workspace_path"],
                    "job_status": payload["runtime"]["job_status"],
                    "job_disposition": payload["runtime"]["job_disposition"],
                    "canonical_ready": payload["runtime"]["canonical_ready"],
                    "reconcile_clean": payload["reconcile"]["clean"],
                    "reused_summary_count": payload["reused_summary_count"],
                    "generated_summary_count": payload["generated_summary_count"],
                    "model_call_count": payload["model_call_count"],
                    "materialization_report_path": payload[
                        "materialization_report_path"
                    ],
                    "materialization_report_sha256": payload[
                        "materialization_report_sha256"
                    ],
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return (
            0
            if payload["runtime"]["canonical_ready"] and payload["reconcile"]["clean"]
            else 2
        )
    raise Stage1ParentError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
