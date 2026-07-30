"""Rebuild the corrected 84-paper PPH Stage 1 parent.

The workflow has three explicit boundaries:

* ``prepare`` rebases the 56 reusable summaries without provider calls.
* ``run-new`` sends only the 28 ``truly_new`` papers to Primary_Reader_API,
  serially, with backup/fallback disabled.
* ``materialize`` closes the unique 84-paper canonical parent only after all
  required DeepSeek summaries pass identity and schema checks.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_interface import get_summary_from_ai_detailed
from config_loader import load_config
from runtime.reconcile import validate_canonical_ai_summary
from services.artifact_registry import file_sha256
from services.job_workspace import atomic_write_json
from services.paper_identity import normalize_doi
from services.summary_reuse import load_summary_records
from summary_schema import normalize_ai_summary

from scripts.pph_stage1_parent import _load_evidence_index, _selected_rows


EXPECTED_CORPUS_COUNT = 84
EXPECTED_REUSABLE_COUNT = 56
EXPECTED_NEW_COUNT = 28
PRIMARY_MODEL = "deepseek-v4-pro"
PRIMARY_PROVIDER = "deepseek"
ALLOWED_DISPOSITIONS = frozenset(
    {
        "reusable_exact",
        "provenance_rebind",
        "ambiguous_manual",
        "truly_new",
    }
)

REUSABLE_SUMMARIES_NAME = "stage1_reusable_56_summaries.json"
REUSABLE_AUDIT_NAME = "stage1_reusable_56_audit.json"
READER_TARGETS_NAME = "stage1_reader_28_targets.json"
READER_LEDGER_NAME = "stage1_reader_request_ledger.jsonl"
READER_RUN_MANIFEST_NAME = "stage1_reader_run_manifest.json"
CANONICAL_SUMMARIES_NAME = "stage1_canonical_84_summaries.json"
CANONICAL_MANIFEST_NAME = "stage1_canonical_84_manifest.json"


class ReaderRebuildError(RuntimeError):
    """Raised when an identity, provider, or provenance boundary is violated."""


@contextmanager
def _exclusive_reader_lock(work_dir: Path):
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / ".reader-api.lock"
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
        else:  # pragma: no cover - Windows is the production target.
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ReaderRebuildError(
            f"another Reader API run owns this work directory: {work_dir}"
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: str | Path) -> Any:
    target = Path(path).expanduser().resolve()
    try:
        return json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReaderRebuildError(f"cannot read JSON: {target}") from exc


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path).expanduser().resolve()
    try:
        lines = target.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReaderRebuildError(f"cannot read JSONL: {target}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ReaderRebuildError(f"blank JSONL line: {target}:{line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReaderRebuildError(
                f"invalid JSONL row: {target}:{line_number}"
            ) from exc
        if not isinstance(value, Mapping):
            raise ReaderRebuildError(
                f"JSONL row is not an object: {target}:{line_number}"
            )
        rows.append(dict(value))
    return rows


def _atomic_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = "".join(
        json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_ledger(path: Path, event: Mapping[str, Any]) -> None:
    rows = _read_jsonl(path) if path.is_file() else []
    rows.append(dict(event))
    _atomic_write_jsonl(path, rows)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_crosswalk(path: Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(path)
    if len(rows) != EXPECTED_CORPUS_COUNT:
        raise ReaderRebuildError("crosswalk must contain exactly 84 physical rows")
    if [int(row.get("index") or 0) for row in rows] != list(
        range(1, EXPECTED_CORPUS_COUNT + 1)
    ):
        raise ReaderRebuildError("crosswalk indices are not contiguous")
    zotero_keys = [str(row.get("zotero_key") or "") for row in rows]
    if len(set(zotero_keys)) != EXPECTED_CORPUS_COUNT or any(
        not key for key in zotero_keys
    ):
        raise ReaderRebuildError("crosswalk Zotero keys are empty or duplicated")
    if any(
        str(row.get("disposition") or "") not in ALLOWED_DISPOSITIONS
        for row in rows
    ):
        raise ReaderRebuildError("crosswalk contains an unsupported disposition")
    counts = Counter(str(row.get("disposition") or "") for row in rows)
    if counts != Counter(
        {
            "reusable_exact": 52,
            "provenance_rebind": 4,
            "truly_new": 28,
        }
    ):
        raise ReaderRebuildError(f"crosswalk disposition drift: {dict(counts)}")
    if any(
        bool(row.get("model_required"))
        != (str(row.get("disposition") or "") == "truly_new")
        for row in rows
    ):
        raise ReaderRebuildError("crosswalk model_required flags are inconsistent")
    return rows


def _load_inputs(
    *,
    selected_manifest: Path,
    crosswalk_path: Path,
    evidence_index_path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    selected_rows = _selected_rows(selected_manifest)
    crosswalk = _load_crosswalk(crosswalk_path)
    if len(selected_rows) != EXPECTED_CORPUS_COUNT:
        raise ReaderRebuildError("selected manifest does not contain 84 papers")
    evidence_by_key = _load_evidence_index(evidence_index_path, selected_rows)
    for selected, row in zip(selected_rows, crosswalk):
        if (
            int(selected.get("source_order") or 0)
            != int(row.get("source_order") or 0)
            or str(selected.get("zotero_parent_key") or "")
            != str(row.get("zotero_key") or "")
            or str(selected.get("pdf_sha256") or "")
            != str(row.get("pdf_sha256") or "")
        ):
            raise ReaderRebuildError(
                f"crosswalk/manifest binding drift at index {row.get('index')}"
            )
        key = str(selected.get("canonical_paper_key") or "")
        evidence = evidence_by_key.get(key)
        if evidence is None:
            raise ReaderRebuildError(f"evidence index is missing {key}")
        if str(evidence.get("source_pdf_sha256") or "") != str(
            selected.get("pdf_sha256") or ""
        ):
            raise ReaderRebuildError(f"evidence PDF binding drift for {key}")
    return selected_rows, crosswalk, evidence_by_key


def _paper_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_authors = row.get("authors") or []
    authors = (
        [str(author) for author in raw_authors]
        if isinstance(raw_authors, (list, tuple))
        else [str(raw_authors)]
    )
    return {
        "title": str(row.get("title") or ""),
        "authors": authors,
        "year": str(row.get("year") or ""),
        "journal": str(row.get("journal") or ""),
        "doi": normalize_doi(row.get("doi")),
    }


def _canonical_paper_info(
    row: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    original: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    zotero_key = str(row.get("zotero_parent_key") or "")
    canonical_key = str(row.get("canonical_paper_key") or "")
    pdf_sha256 = str(row.get("pdf_sha256") or "")
    source_pdf = str(evidence.get("source_pdf") or "")
    descriptor = dict((original or {}).get("source_descriptor") or {})
    descriptor.update(
        {
            "zotero_parent_key": zotero_key,
            "source_pdf_sha256": pdf_sha256,
            "source_pdf_fingerprint": pdf_sha256,
        }
    )
    return {
        **dict(original or {}),
        **_paper_metadata(row),
        "paper_id": zotero_key,
        "zotero_parent_key": zotero_key,
        "canonical_paper_key": canonical_key,
        "source_paper_id": canonical_key,
        "source_mode": "direct",
        "source_pdf": source_pdf,
        "pdf_path": source_pdf,
        "source_pdf_sha256": pdf_sha256,
        "source_pdf_fingerprint": pdf_sha256,
        "source_descriptor": descriptor,
    }


def _bind_ai_metadata(
    ai_summary: Mapping[str, Any],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = normalize_ai_summary(deepcopy(dict(ai_summary)))
    normalized["paper_metadata"] = _paper_metadata(row)
    normalized = normalize_ai_summary(normalized)
    validate_canonical_ai_summary(
        normalized,
        label=f"Stage 1 summary {row.get('canonical_paper_key')}",
    )
    return normalized


def _validate_record_binding(
    record: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
) -> None:
    if str(record.get("status") or "").strip().casefold() != "success":
        raise ReaderRebuildError(
            f"summary is not successful: {row.get('canonical_paper_key')}"
        )
    paper_info = record.get("paper_info")
    if not isinstance(paper_info, Mapping):
        raise ReaderRebuildError("summary has no paper_info object")
    checks = {
        "canonical_paper_key": str(row.get("canonical_paper_key") or ""),
        "zotero_parent_key": str(row.get("zotero_parent_key") or ""),
        "source_pdf_fingerprint": str(row.get("pdf_sha256") or ""),
    }
    for field, expected in checks.items():
        if str(paper_info.get(field) or "") != expected:
            raise ReaderRebuildError(
                f"summary binding mismatch for {field}: "
                f"{row.get('canonical_paper_key')}"
            )
    validate_canonical_ai_summary(
        record.get("ai_summary"),
        label=f"bound Stage 1 summary {row.get('canonical_paper_key')}",
    )


def _historical_record(crosswalk_row: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(
        str(crosswalk_row.get("historical_summary_path") or "")
    ).expanduser().resolve()
    if not path.is_file():
        raise ReaderRebuildError(f"historical summary file is missing: {path}")
    raw_index = crosswalk_row.get("historical_summary_record_index")
    if raw_index is None:
        raise ReaderRebuildError(f"historical record index is missing: {path}")
    index = int(raw_index)
    records = load_summary_records(str(path))
    try:
        record = records[index]
    except IndexError as exc:
        raise ReaderRebuildError(
            f"historical record index is outside file: {path}#{index}"
        ) from exc
    if not isinstance(record, Mapping):
        raise ReaderRebuildError(f"historical record is not an object: {path}#{index}")
    if str(record.get("status") or "").strip().casefold() != "success":
        raise ReaderRebuildError(f"historical record is not successful: {path}#{index}")
    validate_canonical_ai_summary(
        record.get("ai_summary"),
        label=f"historical Stage 1 summary {path}#{index}",
    )
    return dict(record)


def _rebase_reusable_record(
    *,
    selected_row: Mapping[str, Any],
    crosswalk_row: Mapping[str, Any],
    evidence: Mapping[str, Any],
    crosswalk_path: Path,
    evidence_index_path: Path,
) -> dict[str, Any]:
    source = _historical_record(crosswalk_row)
    source_path = Path(
        str(crosswalk_row.get("historical_summary_path") or "")
    ).expanduser().resolve()
    source_index = int(crosswalk_row["historical_summary_record_index"])
    rebound = dict(source)
    rebound["paper_info"] = _canonical_paper_info(
        selected_row,
        evidence,
        original=(
            source.get("paper_info")
            if isinstance(source.get("paper_info"), Mapping)
            else {}
        ),
    )
    rebound["ai_summary"] = _bind_ai_metadata(
        dict(source.get("ai_summary") or {}),
        selected_row,
    )
    disposition = str(crosswalk_row.get("disposition") or "")
    rebound["status"] = "success"
    rebound["source_mode"] = (
        "provenance_rebind"
        if disposition == "provenance_rebind"
        else "historical_reuse"
    )
    rebound["stage1_reuse_receipt"] = {
        "schema_version": "pph-stage1-canonical-reuse-v1",
        "rebound_at": _utc_now(),
        "disposition": disposition,
        "match_type": str(crosswalk_row.get("match_type") or ""),
        "reason": str(crosswalk_row.get("reason") or ""),
        "model_call_count": 0,
        "crosswalk_path": str(crosswalk_path),
        "crosswalk_sha256": file_sha256(crosswalk_path),
        "evidence_index_path": str(evidence_index_path),
        "evidence_index_sha256": file_sha256(evidence_index_path),
        "historical_summary_path": str(source_path),
        "historical_summary_sha256": file_sha256(source_path),
        "historical_summary_record_index": source_index,
        "historical_pdf_sha256": str(
            crosswalk_row.get("historical_pdf_sha256") or ""
        ),
        "current_pdf_sha256": str(selected_row.get("pdf_sha256") or ""),
        "zotero_parent_key": str(selected_row.get("zotero_parent_key") or ""),
    }
    _validate_record_binding(rebound, row=selected_row)
    return rebound


def prepare_reusable(
    *,
    selected_manifest: Path,
    crosswalk_path: Path,
    evidence_index_path: Path,
    work_dir: Path,
) -> dict[str, Any]:
    selected_rows, crosswalk, evidence_by_key = _load_inputs(
        selected_manifest=selected_manifest,
        crosswalk_path=crosswalk_path,
        evidence_index_path=evidence_index_path,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    reusable: list[dict[str, Any]] = []
    reusable_rows: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for selected, crosswalk_row in zip(selected_rows, crosswalk):
        key = str(selected.get("canonical_paper_key") or "")
        disposition = str(crosswalk_row.get("disposition") or "")
        if disposition == "truly_new":
            evidence = evidence_by_key[key]
            targets.append(
                {
                    "index": int(crosswalk_row["index"]),
                    "source_order": int(selected.get("source_order") or 0),
                    "canonical_paper_key": key,
                    "zotero_parent_key": str(
                        selected.get("zotero_parent_key") or ""
                    ),
                    **_paper_metadata(selected),
                    "source_pdf": str(evidence.get("source_pdf") or ""),
                    "pdf_sha256": str(selected.get("pdf_sha256") or ""),
                    "source_pdf_sha256": str(selected.get("pdf_sha256") or ""),
                    "stage1_input_path": str(
                        evidence.get("stage1_input_path") or ""
                    ),
                    "stage1_input_sha256": str(
                        evidence.get("stage1_input_sha256") or ""
                    ),
                    "page_index_path": str(
                        evidence.get("page_index_path") or ""
                    ),
                    "page_index_sha256": str(
                        evidence.get("page_index_sha256") or ""
                    ),
                    "stage1_quality_level": str(
                        evidence.get("stage1_quality_level") or ""
                    ),
                    "stage1_quality_reasons": list(
                        evidence.get("stage1_quality_reasons") or []
                    ),
                    "disposition": disposition,
                    "model_required": True,
                }
            )
            continue
        if disposition not in {"reusable_exact", "provenance_rebind"}:
            raise ReaderRebuildError(
                f"manual disposition blocks preparation: {key}"
            )
        record = _rebase_reusable_record(
            selected_row=selected,
            crosswalk_row=crosswalk_row,
            evidence=evidence_by_key[key],
            crosswalk_path=crosswalk_path,
            evidence_index_path=evidence_index_path,
        )
        reusable.append(record)
        reusable_rows.append(
            {
                "index": int(crosswalk_row["index"]),
                "canonical_paper_key": key,
                "zotero_parent_key": str(
                    selected.get("zotero_parent_key") or ""
                ),
                "pdf_sha256": str(selected.get("pdf_sha256") or ""),
                "disposition": disposition,
                "match_type": str(crosswalk_row.get("match_type") or ""),
                "historical_summary_path": str(
                    crosswalk_row.get("historical_summary_path") or ""
                ),
                "historical_summary_record_index": int(
                    crosswalk_row["historical_summary_record_index"]
                ),
            }
        )

    if len(reusable) != EXPECTED_REUSABLE_COUNT or len(targets) != EXPECTED_NEW_COUNT:
        raise ReaderRebuildError(
            f"prepare counts drifted: reusable={len(reusable)}, new={len(targets)}"
        )
    reusable_path = work_dir / REUSABLE_SUMMARIES_NAME
    targets_path = work_dir / READER_TARGETS_NAME
    audit_path = work_dir / REUSABLE_AUDIT_NAME
    atomic_write_json(str(reusable_path), reusable)
    atomic_write_json(
        str(targets_path),
        {
            "artifact_type": "pph_stage1_reader_targets",
            "schema_version": "pph-stage1-reader-targets-v1",
            "created_at": _utc_now(),
            "provider_executed": False,
            "expected_target_count": EXPECTED_NEW_COUNT,
            "target_count": len(targets),
            "required_route": "Primary_Reader_API",
            "required_provider": PRIMARY_PROVIDER,
            "required_model": PRIMARY_MODEL,
            "concurrency": 1,
            "fallback_allowed": False,
            "targets": targets,
        },
    )
    disposition_counts = dict(
        Counter(row["disposition"] for row in reusable_rows)
    )
    audit = {
        "artifact_type": "pph_stage1_reusable_canonical_rebase",
        "schema_version": "pph-stage1-reusable-canonical-rebase-v1",
        "created_at": _utc_now(),
        "provider_executed": False,
        "crosswalk_path": str(crosswalk_path),
        "crosswalk_sha256": file_sha256(crosswalk_path),
        "selected_manifest_path": str(selected_manifest),
        "selected_manifest_sha256": file_sha256(selected_manifest),
        "evidence_index_path": str(evidence_index_path),
        "evidence_index_sha256": file_sha256(evidence_index_path),
        "reusable_count": len(reusable),
        "target_count": len(targets),
        "disposition_counts": disposition_counts,
        "reusable_summaries_path": str(reusable_path),
        "reusable_summaries_sha256": file_sha256(reusable_path),
        "reader_targets_path": str(targets_path),
        "reader_targets_sha256": file_sha256(targets_path),
        "reusable_rows": reusable_rows,
    }
    atomic_write_json(str(audit_path), audit)
    return {
        "status": "clean",
        "provider_executed": False,
        "reusable_count": len(reusable),
        "target_count": len(targets),
        "reusable_summaries_path": str(reusable_path),
        "reusable_summaries_sha256": file_sha256(reusable_path),
        "reader_targets_path": str(targets_path),
        "reader_targets_sha256": file_sha256(targets_path),
        "audit_path": str(audit_path),
        "audit_sha256": file_sha256(audit_path),
    }


def _read_verified_text(
    path: Path,
    expected_sha256: str,
    *,
    label: str,
    minimum_chars: int = 500,
) -> str:
    if not path.is_file() or file_sha256(path) != expected_sha256:
        raise ReaderRebuildError(f"{label} is missing or stale: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ReaderRebuildError(f"cannot read {label}: {path}") from exc
    if len(text.strip()) < minimum_chars:
        raise ReaderRebuildError(f"{label} is too short: {path}")
    return text


def _build_prompt(
    *,
    target: Mapping[str, Any],
    stage1_text: str,
    prompt_template: str,
) -> str:
    metadata = {
        key: target.get(key)
        for key in ("title", "authors", "year", "journal", "doi")
    }
    verified_metadata = json.dumps(metadata, ensure_ascii=False, indent=2)
    instructions = (
        "【已核验书目信息】\n"
        f"{verified_metadata}\n"
        "paper_metadata 必须逐字段使用以上值；不得从 OCR 噪声改写这些值。\n\n"
    )
    prompt = prompt_template.replace("{{FREE_MODE_CONTEXT}}", "")
    prompt = prompt.replace("{{PAPER_FULL_TEXT}}", stage1_text)
    return instructions + prompt


def _primary_config(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config(str(config_path))
    primary = dict(config.get("Primary_Reader_API") or {})
    if str(primary.get("model") or "") != PRIMARY_MODEL:
        raise ReaderRebuildError(
            f"Primary_Reader_API.model must be {PRIMARY_MODEL}, got "
            f"{primary.get('model')!r}"
        )
    if str(primary.get("provider_family") or "").strip().casefold() != PRIMARY_PROVIDER:
        raise ReaderRebuildError(
            f"Primary_Reader_API.provider_family must be {PRIMARY_PROVIDER}"
        )
    api_key = str(primary.get("api_key") or "")
    if not api_key or api_key == "loaded_from_.env_file":
        raise ReaderRebuildError("Primary_Reader_API key was not loaded")
    return dict(config), primary


def _quality_gate(ai_summary: Mapping[str, Any], *, key: str) -> None:
    quality = ai_summary.get("quality_audit")
    if not isinstance(quality, Mapping):
        raise ReaderRebuildError(f"Reader output has no quality audit: {key}")
    if (
        bool(quality.get("needs_manual_review"))
        or list(quality.get("missing_critical_fields") or [])
        or float(quality.get("completeness_score") or 0.0) < 0.9
    ):
        raise ReaderRebuildError(
            f"Reader output failed completeness gate: {key}; "
            f"quality={dict(quality)}"
        )


def _reader_summary_path(work_dir: Path, index: int) -> Path:
    return work_dir / "stage1_reader_summaries" / f"summary_{index:03d}.json"


def _validate_reader_receipt(
    receipt: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    prompt_sha256: str | None = None,
) -> dict[str, Any]:
    expected = {
        "reader_route": "Primary_Reader_API",
        "provider": PRIMARY_PROVIDER,
        "model": PRIMARY_MODEL,
        "response_model": PRIMARY_MODEL,
        "zotero_parent_key": str(target.get("zotero_parent_key") or ""),
        "source_pdf_sha256": str(target.get("source_pdf_sha256") or ""),
        "stage1_input_sha256": str(target.get("stage1_input_sha256") or ""),
    }
    for field, value in expected.items():
        if str(receipt.get(field) or "") != value:
            raise ReaderRebuildError(
                f"Reader receipt {field} mismatch for {value or '<empty>'}"
            )
    if prompt_sha256 is not None and str(receipt.get("prompt_sha256") or "") != (
        prompt_sha256
    ):
        raise ReaderRebuildError("Reader receipt prompt SHA256 mismatch")
    if (
        int(receipt.get("http_status") or 0) != 200
        or not str(receipt.get("provider_response_id") or "").strip()
        or not str(receipt.get("request_timestamp") or "").strip()
        or int(receipt.get("attempt_count") or 0) < 1
        or int(receipt.get("http_attempt_count") or 0) < 1
        or int(receipt.get("concurrency") or 0) != 1
        or bool(receipt.get("fallback_used"))
        or bool(receipt.get("fallback_allowed"))
        or bool(receipt.get("backup_reader_config_used"))
    ):
        raise ReaderRebuildError("Reader receipt transport evidence is incomplete")

    request_path = Path(str(receipt.get("request_path") or "")).resolve()
    response_path = Path(str(receipt.get("response_path") or "")).resolve()
    request_sha256 = str(receipt.get("request_sha256") or "")
    response_sha256 = str(receipt.get("response_sha256") or "")
    if (
        not request_path.is_file()
        or file_sha256(request_path) != request_sha256
        or not response_path.is_file()
        or file_sha256(response_path) != response_sha256
    ):
        raise ReaderRebuildError("Reader request/response artifact hash mismatch")

    request = _read_json(request_path)
    response = _read_json(response_path)
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise ReaderRebuildError("Reader request/response artifact is not an object")
    request_expected = {
        "request_timestamp": str(receipt.get("request_timestamp") or ""),
        "canonical_paper_key": str(target.get("canonical_paper_key") or ""),
        "zotero_parent_key": expected["zotero_parent_key"],
        "source_pdf_sha256": expected["source_pdf_sha256"],
        "stage1_input_sha256": expected["stage1_input_sha256"],
        "prompt_sha256": str(receipt.get("prompt_sha256") or ""),
        "reader_route": "Primary_Reader_API",
        "provider": PRIMARY_PROVIDER,
        "model": PRIMARY_MODEL,
    }
    for field, value in request_expected.items():
        if str(request.get(field) or "") != value:
            raise ReaderRebuildError(f"Reader request {field} mismatch")
    if (
        bool(request.get("fallback_allowed"))
        or bool(request.get("backup_reader_config_used"))
        or int(request.get("concurrency") or 0) != 1
    ):
        raise ReaderRebuildError("Reader request routing evidence is invalid")
    artifact_proofs = (
        (
            "source PDF",
            "source_pdf",
            None,
            expected["source_pdf_sha256"],
        ),
        (
            "Stage 1 input",
            "stage1_input_path",
            None,
            expected["stage1_input_sha256"],
        ),
        (
            "prompt template",
            "prompt_template_path",
            "prompt_template_sha256",
            None,
        ),
        (
            "system prompt",
            "system_prompt_path",
            "system_prompt_sha256",
            None,
        ),
    )
    for label, path_field, hash_field, expected_hash in artifact_proofs:
        artifact_path = Path(str(request.get(path_field) or "")).resolve()
        artifact_hash = (
            str(request.get(hash_field) or "") if hash_field else str(expected_hash)
        )
        if (
            not artifact_path.is_file()
            or not artifact_hash
            or file_sha256(artifact_path) != artifact_hash
        ):
            raise ReaderRebuildError(f"Reader {label} artifact hash mismatch")

    result = response.get("result")
    if not isinstance(result, Mapping):
        raise ReaderRebuildError("Reader response result metadata is missing")
    response_expected = {
        "request_timestamp": str(receipt.get("request_timestamp") or ""),
        "canonical_paper_key": request_expected["canonical_paper_key"],
        "zotero_parent_key": expected["zotero_parent_key"],
        "source_pdf_sha256": expected["source_pdf_sha256"],
        "provider": PRIMARY_PROVIDER,
        "model": PRIMARY_MODEL,
        "request_path": str(request_path),
        "request_sha256": request_sha256,
    }
    for field, value in response_expected.items():
        if str(response.get(field) or "") != value:
            raise ReaderRebuildError(f"Reader response {field} mismatch")
    if (
        bool(response.get("fallback_used"))
        or bool(response.get("fallback_allowed"))
        or str(result.get("status") or "") != "success"
        or str(result.get("engine_type") or "") != "primary"
        or int(result.get("http_status") or 0) != 200
        or str(result.get("response_model") or "") != PRIMARY_MODEL
        or str(result.get("provider_response_id") or "")
        != str(receipt.get("provider_response_id") or "")
        or int(result.get("attempt_count") or 0)
        != int(receipt.get("attempt_count") or 0)
        or int(result.get("http_attempt_count") or 0)
        != int(receipt.get("http_attempt_count") or 0)
        or int(result.get("http_attempt_count") or 0) < 1
        or not isinstance(response.get("content"), Mapping)
    ):
        raise ReaderRebuildError("Reader response transport evidence is invalid")
    return {
        "request_path": str(request_path),
        "request_sha256": request_sha256,
        "response_path": str(response_path),
        "response_sha256": response_sha256,
        "provider_response_id": str(receipt.get("provider_response_id") or ""),
        "prompt_sha256": str(receipt.get("prompt_sha256") or ""),
    }


def _valid_existing_reader_summary(
    path: Path,
    *,
    target: Mapping[str, Any],
    prompt_sha256: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        return None
    receipt = payload.get("stage1_reader_receipt")
    if not isinstance(receipt, Mapping):
        return None
    try:
        _validate_reader_receipt(
            receipt,
            target=target,
            prompt_sha256=prompt_sha256,
        )
        _validate_record_binding(payload, row=target)
        _quality_gate(
            dict(payload.get("ai_summary") or {}),
            key=str(target.get("canonical_paper_key") or ""),
        )
    except Exception:
        return None
    return dict(payload)


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(path) if path.is_file() else []


def _transport_events(ledger: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in ledger
        if str(row.get("event") or "")
        in {"request_completed", "request_transport_completed"}
    ]


def _ensure_summary_accepted_event(
    *,
    ledger_path: Path,
    summary_path: Path,
    payload: Mapping[str, Any],
    target: Mapping[str, Any],
    prompt_sha256: str,
) -> dict[str, Any]:
    receipt = payload.get("stage1_reader_receipt")
    if not isinstance(receipt, Mapping):
        raise ReaderRebuildError("accepted Reader summary has no receipt")
    proof = _validate_reader_receipt(
        receipt,
        target=target,
        prompt_sha256=prompt_sha256,
    )
    summary_sha256 = file_sha256(summary_path)
    existing = [
        row
        for row in _ledger_rows(ledger_path)
        if str(row.get("event") or "") == "summary_accepted"
        and str(row.get("zotero_parent_key") or "")
        == str(target.get("zotero_parent_key") or "")
        and str(row.get("summary_sha256") or "") == summary_sha256
        and str(row.get("request_sha256") or "") == proof["request_sha256"]
        and str(row.get("response_sha256") or "") == proof["response_sha256"]
    ]
    if existing:
        return dict(existing[-1])
    event = {
        "event": "summary_accepted",
        "timestamp": _utc_now(),
        "request_timestamp": str(receipt.get("request_timestamp") or ""),
        "index": int(target.get("index") or 0),
        "canonical_paper_key": str(target.get("canonical_paper_key") or ""),
        "zotero_parent_key": str(target.get("zotero_parent_key") or ""),
        "source_pdf_sha256": str(target.get("source_pdf_sha256") or ""),
        "provider": PRIMARY_PROVIDER,
        "model": PRIMARY_MODEL,
        "response_model": PRIMARY_MODEL,
        "http_status": 200,
        "provider_response_id": proof["provider_response_id"],
        "attempt_count": int(receipt.get("attempt_count") or 0),
        "http_attempt_count": int(receipt.get("http_attempt_count") or 0),
        "prompt_sha256": prompt_sha256,
        "request_path": proof["request_path"],
        "request_sha256": proof["request_sha256"],
        "response_path": proof["response_path"],
        "response_sha256": proof["response_sha256"],
        "summary_path": str(summary_path),
        "summary_sha256": summary_sha256,
        "fallback_allowed": False,
        "fallback_used": False,
        "evidence_source": "validated_summary_artifacts",
    }
    _append_ledger(ledger_path, event)
    return event


def _write_reader_manifest(
    *,
    work_dir: Path,
    targets_path: Path,
    config_path: Path,
    config: Mapping[str, Any],
    primary_max_tokens: int,
) -> dict[str, Any]:
    targets_payload = _read_json(targets_path)
    targets = list((targets_payload or {}).get("targets") or [])
    successes: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        summary_path = _reader_summary_path(
            work_dir,
            int(target.get("index") or 0),
        )
        if not summary_path.is_file():
            continue
        payload = _read_json(summary_path)
        if not isinstance(payload, Mapping):
            continue
        receipt = payload.get("stage1_reader_receipt")
        if not isinstance(receipt, Mapping):
            continue
        try:
            _validate_reader_receipt(receipt, target=target)
            _validate_record_binding(payload, row=target)
            _quality_gate(
                dict(payload.get("ai_summary") or {}),
                key=str(target.get("canonical_paper_key") or ""),
            )
        except Exception:
            continue
        successes.append(
            {
                "index": int(target.get("index") or 0),
                "zotero_parent_key": str(
                    target.get("zotero_parent_key") or ""
                ),
                "summary_path": str(summary_path),
                "summary_sha256": file_sha256(summary_path),
                "request_timestamp": str(
                    receipt.get("request_timestamp") or ""
                ),
                "provider_response_id": str(
                    receipt.get("provider_response_id") or ""
                ),
                "http_status": receipt.get("http_status"),
                "response_model": str(receipt.get("response_model") or ""),
                "request_sha256": str(receipt.get("request_sha256") or ""),
                "response_sha256": str(receipt.get("response_sha256") or ""),
            }
        )
    ledger_path = work_dir / READER_LEDGER_NAME
    ledger = _ledger_rows(ledger_path)
    completed = _transport_events(ledger)
    accepted = [
        row for row in ledger if str(row.get("event") or "") == "summary_accepted"
    ]
    failed = [
        row
        for row in completed
        if str(row.get("status") or "") != "success"
    ]
    rejections = []
    for rejection_path in sorted(
        (work_dir / "stage1_reader_responses").glob("*_rejection.json")
    ):
        rejection = _read_json(rejection_path)
        if not isinstance(rejection, Mapping):
            continue
        rejections.append(
            {
                **dict(rejection),
                "rejection_path": str(rejection_path),
                "rejection_sha256": file_sha256(rejection_path),
            }
        )
    fallback_events = [row for row in ledger if bool(row.get("fallback_used"))]
    if fallback_events:
        raise ReaderRebuildError("reader ledger contains a forbidden fallback")
    actual_http_attempts = sum(
        int(row.get("http_attempt_count") or 0) for row in completed
    )
    validator = dict(config.get("Validator_API") or {})
    manifest = {
        "artifact_type": "pph_stage1_reader_run_manifest",
        "schema_version": "pph-stage1-reader-run-manifest-v1",
        "updated_at": _utc_now(),
        "status": (
            "clean"
            if len(successes) == EXPECTED_NEW_COUNT
            else "incomplete"
        ),
        "expected_paper_count": EXPECTED_NEW_COUNT,
        "successful_paper_count": len(successes),
        "remaining_paper_count": EXPECTED_NEW_COUNT - len(successes),
        "failed_request_count": len(failed) + len(rejections),
        "transport_failure_count": len(failed),
        "quality_rejection_count": len(rejections),
        "completed_request_count": len(completed),
        "accepted_summary_event_count": len(accepted),
        "actual_http_attempt_count": actual_http_attempts,
        "concurrency": 1,
        "reader_route": "Primary_Reader_API",
        "provider": PRIMARY_PROVIDER,
        "model": PRIMARY_MODEL,
        "primary_max_tokens": primary_max_tokens,
        "fallback_allowed": False,
        "fallback_count": 0,
        "config_path": str(config_path),
        "config_sha256": file_sha256(config_path),
        "validator_model_observed": str(validator.get("model") or ""),
        "targets_path": str(targets_path),
        "targets_sha256": file_sha256(targets_path),
        "ledger_path": str(ledger_path),
        "ledger_sha256": file_sha256(ledger_path) if ledger_path.is_file() else "",
        "successes": sorted(successes, key=lambda item: item["index"]),
        "failures": [*failed, *rejections],
    }
    manifest_path = work_dir / READER_RUN_MANIFEST_NAME
    atomic_write_json(str(manifest_path), manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def run_new(
    *,
    selected_manifest: Path,
    crosswalk_path: Path,
    evidence_index_path: Path,
    work_dir: Path,
    config_path: Path,
    prompt_template_path: Path,
    system_prompt_path: Path,
    max_papers: int = 0,
    primary_max_tokens: int = 8_000,
) -> dict[str, Any]:
    prepare_reusable(
        selected_manifest=selected_manifest,
        crosswalk_path=crosswalk_path,
        evidence_index_path=evidence_index_path,
        work_dir=work_dir,
    )
    config, primary = _primary_config(config_path)
    api_parameters = dict(config.get("API_Parameters") or {})
    configured_max_tokens = int(api_parameters.get("primary_max_tokens") or 0)
    effective_max_tokens = max(configured_max_tokens, primary_max_tokens)
    api_parameters["primary_max_tokens"] = str(effective_max_tokens)
    config["API_Parameters"] = api_parameters
    targets_path = work_dir / READER_TARGETS_NAME
    targets_payload = _read_json(targets_path)
    targets = list((targets_payload or {}).get("targets") or [])
    if len(targets) != EXPECTED_NEW_COUNT:
        raise ReaderRebuildError("reader target manifest does not contain 28 papers")
    prompt_template = _read_verified_text(
        prompt_template_path,
        file_sha256(prompt_template_path),
        label="Stage 1 prompt template",
        minimum_chars=1,
    )
    system_prompt = _read_verified_text(
        system_prompt_path,
        file_sha256(system_prompt_path),
        label="Stage 1 system prompt",
        minimum_chars=1,
    )
    logger = logging.getLogger("pph_stage1_reader_rebuild")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "stage1_reader_requests").mkdir(parents=True, exist_ok=True)
    (work_dir / "stage1_reader_responses").mkdir(parents=True, exist_ok=True)
    (work_dir / "stage1_reader_summaries").mkdir(parents=True, exist_ok=True)
    executed = 0

    for target_value in targets:
        if not isinstance(target_value, Mapping):
            raise ReaderRebuildError("reader target is not an object")
        target = dict(target_value)
        index = int(target.get("index") or 0)
        key = str(target.get("canonical_paper_key") or "")
        zotero_key = str(target.get("zotero_parent_key") or "")
        stage1_input_path = Path(str(target.get("stage1_input_path") or "")).resolve()
        stage1_text = _read_verified_text(
            stage1_input_path,
            str(target.get("stage1_input_sha256") or ""),
            label=f"Stage 1 input {key}",
        )
        prompt = _build_prompt(
            target=target,
            stage1_text=stage1_text,
            prompt_template=prompt_template,
        )
        prompt_sha256 = _sha256_text(prompt)
        summary_path = _reader_summary_path(work_dir, index)
        existing = _valid_existing_reader_summary(
            summary_path,
            target=target,
            prompt_sha256=prompt_sha256,
        )
        if existing is not None:
            _ensure_summary_accepted_event(
                ledger_path=work_dir / READER_LEDGER_NAME,
                summary_path=summary_path,
                payload=existing,
                target=target,
                prompt_sha256=prompt_sha256,
            )
            logger.info(
                "Reader resume accepted index=%03d zotero=%s model=%s",
                index,
                zotero_key,
                PRIMARY_MODEL,
            )
            continue
        if max_papers > 0 and executed >= max_papers:
            break

        prior_completed = [
            row
            for row in _ledger_rows(work_dir / READER_LEDGER_NAME)
            if str(row.get("event") or "")
            in {"request_completed", "request_transport_completed"}
            and str(row.get("zotero_parent_key") or "") == zotero_key
        ]
        attempt_number = len(prior_completed) + 1
        request_timestamp = _utc_now()
        request_path = (
            work_dir
            / "stage1_reader_requests"
            / f"request_{index:03d}_attempt_{attempt_number:02d}.json"
        )
        response_path = (
            work_dir
            / "stage1_reader_responses"
            / f"response_{index:03d}_attempt_{attempt_number:02d}.json"
        )
        request = {
            "artifact_type": "pph_stage1_reader_request",
            "schema_version": "pph-stage1-reader-request-v1",
            "request_timestamp": request_timestamp,
            "index": index,
            "canonical_paper_key": key,
            "zotero_parent_key": zotero_key,
            "source_pdf": str(target.get("source_pdf") or ""),
            "source_pdf_sha256": str(target.get("source_pdf_sha256") or ""),
            "stage1_input_path": str(stage1_input_path),
            "stage1_input_sha256": str(
                target.get("stage1_input_sha256") or ""
            ),
            "prompt_template_path": str(prompt_template_path),
            "prompt_template_sha256": file_sha256(prompt_template_path),
            "system_prompt_path": str(system_prompt_path),
            "system_prompt_sha256": file_sha256(system_prompt_path),
            "prompt_sha256": prompt_sha256,
            "prompt_chars": len(prompt),
            "reader_route": "Primary_Reader_API",
            "provider": PRIMARY_PROVIDER,
            "model": PRIMARY_MODEL,
            "api_base": str(primary.get("api_base") or ""),
            "endpoint_type": str(primary.get("endpoint_type") or ""),
            "concurrency": 1,
            "fallback_allowed": False,
            "backup_reader_config_used": False,
            "transport_retry_attempts": 1,
            "primary_max_tokens": effective_max_tokens,
        }
        atomic_write_json(str(request_path), request)
        _append_ledger(
            work_dir / READER_LEDGER_NAME,
            {
                "event": "request_started",
                "timestamp": request_timestamp,
                "index": index,
                "attempt_number": attempt_number,
                "canonical_paper_key": key,
                "zotero_parent_key": zotero_key,
                "source_pdf_sha256": request["source_pdf_sha256"],
                "provider": PRIMARY_PROVIDER,
                "model": PRIMARY_MODEL,
                "request_path": str(request_path),
                "request_sha256": file_sha256(request_path),
                "fallback_allowed": False,
                "fallback_used": False,
            },
        )
        logger.info(
            "Reader request start index=%03d zotero=%s provider=%s model=%s",
            index,
            zotero_key,
            PRIMARY_PROVIDER,
            PRIMARY_MODEL,
        )
        result = get_summary_from_ai_detailed(
            prompt,
            primary,
            {},
            engine_type="primary",
            logger=logger,
            config=config,
            retry_attempts=1,
            allow_rate_limiter_switch=False,
            system_prompt=system_prompt,
        )
        completed_at = _utc_now()
        result_metadata = {
            key_name: result.get(key_name)
            for key_name in (
                "status",
                "error_kind",
                "http_status",
                "provider_code",
                "message",
                "engine_type",
                "finish_reason",
                "provider_response_id",
                "response_model",
                "usage",
                "attempt_count",
                "http_attempt_count",
                "api_url",
            )
        }
        response = {
            "artifact_type": "pph_stage1_reader_response",
            "schema_version": "pph-stage1-reader-response-v1",
            "request_timestamp": request_timestamp,
            "completed_at": completed_at,
            "index": index,
            "attempt_number": attempt_number,
            "canonical_paper_key": key,
            "zotero_parent_key": zotero_key,
            "source_pdf_sha256": request["source_pdf_sha256"],
            "provider": PRIMARY_PROVIDER,
            "model": PRIMARY_MODEL,
            "fallback_allowed": False,
            "fallback_used": False,
            "request_path": str(request_path),
            "request_sha256": file_sha256(request_path),
            "result": result_metadata,
            "content": result.get("content"),
        }
        atomic_write_json(str(response_path), response)
        response_sha256 = file_sha256(response_path)
        completion_event = {
            "event": "request_transport_completed",
            "timestamp": completed_at,
            "request_timestamp": request_timestamp,
            "index": index,
            "attempt_number": attempt_number,
            "canonical_paper_key": key,
            "zotero_parent_key": zotero_key,
            "source_pdf_sha256": request["source_pdf_sha256"],
            "provider": PRIMARY_PROVIDER,
            "model": PRIMARY_MODEL,
            "engine_type": str(result.get("engine_type") or ""),
            "status": str(result.get("status") or ""),
            "error_kind": str(result.get("error_kind") or ""),
            "http_status": result.get("http_status"),
            "provider_response_id": str(
                result.get("provider_response_id") or ""
            ),
            "response_model": str(result.get("response_model") or ""),
            "attempt_count": int(result.get("attempt_count") or 0),
            "http_attempt_count": int(result.get("http_attempt_count") or 0),
            "request_path": str(request_path),
            "request_sha256": file_sha256(request_path),
            "response_path": str(response_path),
            "response_sha256": response_sha256,
            "fallback_allowed": False,
            "fallback_used": False,
        }
        _append_ledger(work_dir / READER_LEDGER_NAME, completion_event)
        executed += 1

        if (
            result.get("status") != "success"
            or str(result.get("engine_type") or "") != "primary"
            or int(result.get("http_status") or 0) != 200
            or not isinstance(result.get("content"), Mapping)
        ):
            logger.error(
                "Reader request failed index=%03d zotero=%s status=%s error=%s",
                index,
                zotero_key,
                result.get("status"),
                result.get("message") or result.get("error_kind"),
            )
            _write_reader_manifest(
                work_dir=work_dir,
                targets_path=targets_path,
                config_path=config_path,
                config=config,
                primary_max_tokens=effective_max_tokens,
            )
            continue

        try:
            ai_summary = _bind_ai_metadata(
                dict(result.get("content") or {}),
                target,
            )
            _quality_gate(ai_summary, key=key)
            paper_info = _canonical_paper_info(target, target)
            record = {
                "paper_info": paper_info,
                "status": "success",
                "ai_summary": ai_summary,
                "processing_time": completed_at,
                "source_mode": "reader_api",
                "provider": PRIMARY_PROVIDER,
                "model": PRIMARY_MODEL,
                "model_used": PRIMARY_MODEL,
                "request_timestamp": request_timestamp,
                "stage1_reader_receipt": {
                    "schema_version": "pph-stage1-reader-receipt-v1",
                    "reader_route": "Primary_Reader_API",
                    "provider": PRIMARY_PROVIDER,
                    "model": PRIMARY_MODEL,
                    "request_timestamp": request_timestamp,
                    "completed_at": completed_at,
                    "zotero_parent_key": zotero_key,
                    "source_pdf_sha256": request["source_pdf_sha256"],
                    "stage1_input_sha256": request["stage1_input_sha256"],
                    "prompt_sha256": prompt_sha256,
                    "prompt_template_sha256": request[
                        "prompt_template_sha256"
                    ],
                    "system_prompt_sha256": request["system_prompt_sha256"],
                    "request_path": str(request_path),
                    "request_sha256": file_sha256(request_path),
                    "response_path": str(response_path),
                    "response_sha256": response_sha256,
                    "http_status": result.get("http_status"),
                    "provider_response_id": str(
                        result.get("provider_response_id") or ""
                    ),
                    "response_model": str(result.get("response_model") or ""),
                    "usage": dict(result.get("usage") or {}),
                    "attempt_count": int(result.get("attempt_count") or 0),
                    "http_attempt_count": int(
                        result.get("http_attempt_count") or 0
                    ),
                    "api_url": str(result.get("api_url") or ""),
                    "concurrency": 1,
                    "fallback_allowed": False,
                    "fallback_used": False,
                    "backup_reader_config_used": False,
                },
            }
            _validate_record_binding(record, row=target)
            atomic_write_json(str(summary_path), record)
            _ensure_summary_accepted_event(
                ledger_path=work_dir / READER_LEDGER_NAME,
                summary_path=summary_path,
                payload=record,
                target=target,
                prompt_sha256=prompt_sha256,
            )
            logger.info(
                "Reader request success index=%03d zotero=%s response_id=%s",
                index,
                zotero_key,
                result.get("provider_response_id") or "",
            )
        except Exception as exc:
            logger.error(
                "Reader output rejected index=%03d zotero=%s reason=%s",
                index,
                zotero_key,
                exc,
            )
            rejection_path = response_path.with_name(
                response_path.stem + "_rejection.json"
            )
            atomic_write_json(
                str(rejection_path),
                {
                    "artifact_type": "pph_stage1_reader_output_rejection",
                    "schema_version": "pph-stage1-reader-output-rejection-v1",
                    "created_at": _utc_now(),
                    "index": index,
                    "canonical_paper_key": key,
                    "zotero_parent_key": zotero_key,
                    "provider": PRIMARY_PROVIDER,
                    "model": PRIMARY_MODEL,
                    "response_path": str(response_path),
                    "response_sha256": response_sha256,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "fallback_used": False,
                },
            )
        _write_reader_manifest(
            work_dir=work_dir,
            targets_path=targets_path,
            config_path=config_path,
            config=config,
            primary_max_tokens=effective_max_tokens,
        )

    manifest = _write_reader_manifest(
        work_dir=work_dir,
        targets_path=targets_path,
        config_path=config_path,
        config=config,
        primary_max_tokens=effective_max_tokens,
    )
    return {
        "status": manifest["status"],
        "executed_this_run": executed,
        "successful_paper_count": manifest["successful_paper_count"],
        "remaining_paper_count": manifest["remaining_paper_count"],
        "failed_request_count": manifest["failed_request_count"],
        "completed_request_count": manifest["completed_request_count"],
        "actual_http_attempt_count": manifest["actual_http_attempt_count"],
        "fallback_count": manifest["fallback_count"],
        "provider": manifest["provider"],
        "model": manifest["model"],
        "validator_model_observed": manifest["validator_model_observed"],
        "manifest_path": manifest["manifest_path"],
        "manifest_sha256": file_sha256(manifest["manifest_path"]),
    }


def materialize(
    *,
    selected_manifest: Path,
    crosswalk_path: Path,
    evidence_index_path: Path,
    work_dir: Path,
) -> dict[str, Any]:
    selected_rows, crosswalk, _ = _load_inputs(
        selected_manifest=selected_manifest,
        crosswalk_path=crosswalk_path,
        evidence_index_path=evidence_index_path,
    )
    reusable_path = work_dir / REUSABLE_SUMMARIES_NAME
    if not reusable_path.is_file():
        raise ReaderRebuildError("reusable summary file has not been prepared")
    reusable_payload = _read_json(reusable_path)
    if not isinstance(reusable_payload, list):
        raise ReaderRebuildError("reusable summary file is not an array")
    reusable_by_key = {
        str((record.get("paper_info") or {}).get("canonical_paper_key") or ""): dict(
            record
        )
        for record in reusable_payload
        if isinstance(record, Mapping)
    }
    if len(reusable_by_key) != EXPECTED_REUSABLE_COUNT:
        raise ReaderRebuildError("reusable summary identities are not unique")
    targets_path = work_dir / READER_TARGETS_NAME
    targets_payload = _read_json(targets_path)
    targets = list((targets_payload or {}).get("targets") or [])
    targets_by_key = {
        str(target.get("canonical_paper_key") or ""): dict(target)
        for target in targets
        if isinstance(target, Mapping)
    }
    if len(targets_by_key) != EXPECTED_NEW_COUNT:
        raise ReaderRebuildError("Reader target identities are not unique")

    canonical: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    current_reader_proofs: list[dict[str, Any]] = []
    for selected, crosswalk_row in zip(selected_rows, crosswalk):
        key = str(selected.get("canonical_paper_key") or "")
        disposition = str(crosswalk_row.get("disposition") or "")
        if disposition == "truly_new":
            source_path = _reader_summary_path(
                work_dir,
                int(crosswalk_row["index"]),
            )
            if not source_path.is_file():
                raise ReaderRebuildError(f"Reader summary is missing: {key}")
            source = _read_json(source_path)
            if not isinstance(source, Mapping):
                raise ReaderRebuildError(f"Reader summary is not an object: {key}")
            receipt = source.get("stage1_reader_receipt")
            reader_target = targets_by_key.get(key)
            if not isinstance(receipt, Mapping) or reader_target is None:
                raise ReaderRebuildError(f"Reader receipt is invalid: {key}")
            proof = _validate_reader_receipt(receipt, target=reader_target)
            _quality_gate(
                dict(source.get("ai_summary") or {}),
                key=key,
            )
            current_reader_proofs.append(
                {
                    "zotero_parent_key": str(
                        reader_target.get("zotero_parent_key") or ""
                    ),
                    "summary_sha256": file_sha256(source_path),
                    **proof,
                }
            )
            source_kind = "newly_generated"
        else:
            source = reusable_by_key.get(key)
            if source is None:
                raise ReaderRebuildError(f"reusable summary is missing: {key}")
            source_path = reusable_path
            source_kind = disposition
        record = dict(source)
        _validate_record_binding(record, row=selected)
        canonical.append(record)
        manifest_rows.append(
            {
                "index": int(crosswalk_row["index"]),
                "canonical_paper_key": key,
                "zotero_parent_key": str(
                    selected.get("zotero_parent_key") or ""
                ),
                "title": str(selected.get("title") or ""),
                "pdf_sha256": str(selected.get("pdf_sha256") or ""),
                "disposition": disposition,
                "source_kind": source_kind,
                "summary_source_path": str(source_path),
                "summary_source_sha256": file_sha256(source_path),
            }
        )

    if len(canonical) != EXPECTED_CORPUS_COUNT:
        raise ReaderRebuildError("canonical parent does not contain 84 summaries")
    canonical_keys = [
        str((record.get("paper_info") or {}).get("canonical_paper_key") or "")
        for record in canonical
    ]
    zotero_keys = [
        str((record.get("paper_info") or {}).get("zotero_parent_key") or "")
        for record in canonical
    ]
    if (
        len(set(canonical_keys)) != EXPECTED_CORPUS_COUNT
        or len(set(zotero_keys)) != EXPECTED_CORPUS_COUNT
    ):
        raise ReaderRebuildError("canonical parent identities are duplicated")

    ledger_path = work_dir / READER_LEDGER_NAME
    ledger = _ledger_rows(ledger_path)
    completed = _transport_events(ledger)
    accepted_events = [
        row for row in ledger if str(row.get("event") or "") == "summary_accepted"
    ]
    successful_request_papers: set[str] = set()
    for proof in current_reader_proofs:
        zotero_key = str(proof["zotero_parent_key"])
        accepted_match = any(
            str(row.get("zotero_parent_key") or "") == zotero_key
            and str(row.get("summary_sha256") or "")
            == str(proof["summary_sha256"])
            and str(row.get("request_sha256") or "")
            == str(proof["request_sha256"])
            and str(row.get("response_sha256") or "")
            == str(proof["response_sha256"])
            and str(row.get("provider") or "") == PRIMARY_PROVIDER
            and str(row.get("model") or "") == PRIMARY_MODEL
            and str(row.get("response_model") or "") == PRIMARY_MODEL
            and int(row.get("http_status") or 0) == 200
            and str(row.get("provider_response_id") or "")
            == str(proof["provider_response_id"])
            and str(row.get("prompt_sha256") or "")
            == str(proof["prompt_sha256"])
            and not bool(row.get("fallback_used"))
            for row in accepted_events
        )
        transport_match = any(
            str(row.get("zotero_parent_key") or "") == zotero_key
            and str(row.get("request_sha256") or "")
            == str(proof["request_sha256"])
            and str(row.get("response_sha256") or "")
            == str(proof["response_sha256"])
            and str(row.get("status") or "") == "success"
            and str(row.get("provider") or "") == PRIMARY_PROVIDER
            and str(row.get("model") or "") == PRIMARY_MODEL
            and str(row.get("response_model") or "") == PRIMARY_MODEL
            and int(row.get("http_status") or 0) == 200
            and str(row.get("provider_response_id") or "")
            == str(proof["provider_response_id"])
            and not bool(row.get("fallback_used"))
            for row in completed
        )
        if not accepted_match or not transport_match:
            raise ReaderRebuildError(
                f"Reader ledger evidence does not match accepted summary: {zotero_key}"
            )
        successful_request_papers.add(zotero_key)
    fallback_events = [row for row in ledger if bool(row.get("fallback_used"))]
    if len(successful_request_papers) != EXPECTED_NEW_COUNT or fallback_events:
        raise ReaderRebuildError(
            "Reader request ledger does not prove 28 primary-only successes"
        )

    canonical_path = work_dir / CANONICAL_SUMMARIES_NAME
    atomic_write_json(str(canonical_path), canonical)
    manifest = {
        "artifact_type": "pph_stage1_canonical_parent",
        "schema_version": "pph-stage1-canonical-parent-v1",
        "created_at": _utc_now(),
        "status": "clean",
        "canonical_ready": True,
        "expected_corpus_count": EXPECTED_CORPUS_COUNT,
        "canonical_summary_count": len(canonical),
        "unique_canonical_paper_key_count": len(set(canonical_keys)),
        "unique_zotero_parent_key_count": len(set(zotero_keys)),
        "reusable_exact_count": 52,
        "provenance_rebind_count": 4,
        "newly_generated_count": 28,
        "model_required_paper_count": 28,
        "reader_success_paper_count": len(successful_request_papers),
        "reader_completed_request_count": len(completed),
        "reader_accepted_summary_event_count": len(accepted_events),
        "actual_http_attempt_count": sum(
            int(row.get("http_attempt_count") or 0) for row in completed
        ),
        "reader_route": "Primary_Reader_API",
        "reader_provider": PRIMARY_PROVIDER,
        "reader_model": PRIMARY_MODEL,
        "reader_concurrency": 1,
        "fallback_allowed": False,
        "fallback_count": 0,
        "selected_manifest_path": str(selected_manifest),
        "selected_manifest_sha256": file_sha256(selected_manifest),
        "crosswalk_path": str(crosswalk_path),
        "crosswalk_sha256": file_sha256(crosswalk_path),
        "evidence_index_path": str(evidence_index_path),
        "evidence_index_sha256": file_sha256(evidence_index_path),
        "reusable_summaries_path": str(reusable_path),
        "reusable_summaries_sha256": file_sha256(reusable_path),
        "reader_ledger_path": str(ledger_path),
        "reader_ledger_sha256": file_sha256(ledger_path),
        "canonical_summaries_path": str(canonical_path),
        "canonical_summaries_sha256": file_sha256(canonical_path),
        "papers": manifest_rows,
    }
    manifest_path = work_dir / CANONICAL_MANIFEST_NAME
    atomic_write_json(str(manifest_path), manifest)
    return {
        "status": "clean",
        "canonical_ready": True,
        "canonical_summary_count": len(canonical),
        "reusable_summary_count": EXPECTED_REUSABLE_COUNT,
        "newly_generated_count": EXPECTED_NEW_COUNT,
        "reader_model": PRIMARY_MODEL,
        "fallback_count": 0,
        "canonical_summaries_path": str(canonical_path),
        "canonical_summaries_sha256": file_sha256(canonical_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
    }


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--selected-manifest", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--evidence-index", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild the corrected primary-only 84-paper PPH Stage 1 parent."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    _common_arguments(prepare)

    run = subparsers.add_parser("run-new")
    _common_arguments(run)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument(
        "--prompt-template",
        type=Path,
        default=Path("prompts/optimized_prompt_analyze_router.txt"),
    )
    run.add_argument(
        "--system-prompt",
        type=Path,
        default=Path("prompts/prompt_system_analyze.txt"),
    )
    run.add_argument(
        "--max-papers",
        type=int,
        default=0,
        help="Optional bounded smoke/resume batch; 0 processes every remaining target.",
    )
    run.add_argument(
        "--primary-max-tokens",
        type=int,
        default=8_000,
        help="In-memory Stage 1 output limit; never changes config.ini.",
    )

    materialize_parser = subparsers.add_parser("materialize")
    _common_arguments(materialize_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    common = {
        "selected_manifest": args.selected_manifest.expanduser().resolve(),
        "crosswalk_path": args.crosswalk.expanduser().resolve(),
        "evidence_index_path": args.evidence_index.expanduser().resolve(),
        "work_dir": args.work_dir.expanduser().resolve(),
    }
    if args.command == "prepare":
        result = prepare_reusable(**common)
    elif args.command == "run-new":
        with _exclusive_reader_lock(common["work_dir"]):
            result = run_new(
                **common,
                config_path=args.config.expanduser().resolve(),
                prompt_template_path=args.prompt_template.expanduser().resolve(),
                system_prompt_path=args.system_prompt.expanduser().resolve(),
                max_papers=max(0, int(args.max_papers)),
                primary_max_tokens=max(1, int(args.primary_max_tokens)),
            )
    elif args.command == "materialize":
        result = materialize(**common)
    else:
        raise ReaderRebuildError(f"unsupported command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
