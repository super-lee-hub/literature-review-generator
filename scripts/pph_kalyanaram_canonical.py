from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.reconcile import validate_canonical_ai_summary
from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError
from runtime.stage_contracts import PaperWorkItem
from services.job_workspace import atomic_write_json, utc_now_iso
from services.stage1_input_completeness import build_completeness_metrics
from summary_schema import normalize_ai_summary


TARGET_DOI = "10.1287/mksc.14.3.g161"
EXPECTED_SOURCE_SHA256 = (
    "2ec00e6240bb8309b2901a542df62a55a81ff5d5efcd43ef8ae0997b5b36c1d5"
)
PROJECT_NAME = "pph_supplemental_kalyanaram_reference_price"
ZOTERO_PARENT_KEY = "YMCVAMMM"
ZOTERO_ATTACHMENT_KEY = "SN9S4LYQ"
EXPECTED_PAGE_COUNT = 10

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_PDF = Path(
    r"D:\zotero library\营销科学与消费者洞察\消费者机制与体验\消费者判断与决策"
    r"\决策偏差与启发式\损失厌恶\1995 - Marketing Science - Kalyanaram et.al"
    r" - Empirical Generalizations from Reference Price Res.pdf"
)
DEFAULT_SOURCE_REPORT = (
    REPO_ROOT
    / "output"
    / "pph_supplemental_sources"
    / "kalyanaram_1995"
    / "zotero_report.txt"
)
DEFAULT_EVIDENCE_DIR = (
    REPO_ROOT
    / "output"
    / "pph_supplemental_sources"
    / "kalyanaram_1995"
    / "evidence_v1"
)
DEFAULT_OCR_DIR = (
    REPO_ROOT / "tmp" / "pdfs" / "kalyanaram_1995" / "ocr_pages"
)
DEFAULT_DRAFT = REPO_ROOT / "tmp" / "kalyanaram_supplemental_stage1_draft.json"


class KalyanaramCanonicalError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidencePaths:
    markdown_path: Path
    chunks_path: Path
    page_index_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "markdown_path": str(self.markdown_path),
            "chunks_path": str(self.chunks_path),
            "page_index_path": str(self.page_index_path),
        }


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _expected_ocr_pages(ocr_dir: Path) -> list[Path]:
    expected = [
        ocr_dir / f"page-{page_number:02d}.txt"
        for page_number in range(1, EXPECTED_PAGE_COUNT + 1)
    ]
    actual = sorted(ocr_dir.glob("page-*.txt"))
    if actual != expected:
        missing = [str(path) for path in expected if not path.is_file()]
        unexpected = [str(path) for path in actual if path not in expected]
        raise KalyanaramCanonicalError(
            "OCR page set must be exactly page-01.txt through page-10.txt; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return expected


def build_ocr_evidence(
    ocr_dir: str | Path,
    evidence_dir: str | Path,
) -> EvidencePaths:
    source_dir = Path(ocr_dir).expanduser().resolve()
    target_dir = Path(evidence_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise KalyanaramCanonicalError(f"OCR directory does not exist: {source_dir}")

    page_texts: list[str] = []
    for path in _expected_ocr_pages(source_dir):
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
        if not text:
            raise KalyanaramCanonicalError(f"OCR page is empty: {path}")
        page_texts.append(text)

    markdown_parts = [
        "# Kalyanaram and Winer (1995) OCR evidence",
        "",
        (
            "Source: Empirical Generalizations from Reference Price Research "
            f"(DOI {TARGET_DOI})."
        ),
        "",
        (
            "Extraction: Tesseract 5.5.0, English, page-level OCR. "
            "The ten rendered pages were visually read back before canonicalization."
        ),
        "",
    ]
    chunks: list[dict[str, Any]] = []
    page_index: list[dict[str, Any]] = []
    for page_number, text in enumerate(page_texts, start=1):
        label = f"PDF page {page_number:02d}"
        markdown_parts.extend([f"## {label}", "", text, ""])
        chunks.append(
            {
                "chunk_id": f"kalyanaram-1995-page-{page_number:02d}",
                "page_number": page_number,
                "page_label": label,
                "text": text,
                "char_count": len(text),
                "extraction_method": "tesseract-5.5.0-eng-psm3",
                "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
        page_index.append(
            {
                "page_number": page_number,
                "page_label": label,
                "chunk_id": f"kalyanaram-1995-page-{page_number:02d}",
                "char_count": len(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )

    markdown = "\n".join(markdown_parts).rstrip() + "\n"
    markdown_path = (target_dir / "normalized.md").resolve()
    chunks_path = (target_dir / "chunks.json").resolve()
    page_index_path = (target_dir / "page_index.json").resolve()
    _atomic_write_text(markdown_path, markdown)
    atomic_write_json(str(chunks_path), chunks)
    atomic_write_json(str(page_index_path), page_index)

    return EvidencePaths(
        markdown_path=markdown_path,
        chunks_path=chunks_path,
        page_index_path=page_index_path,
    )


def _dedupe_strings(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _materialize_ai_summary(
    draft_ai_summary: Any,
    paper_info: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = normalize_ai_summary(draft_ai_summary)
    metadata = dict(normalized.get("paper_metadata") or {})
    for field in ("title", "authors", "year", "journal", "doi"):
        if paper_info.get(field):
            metadata[field] = paper_info[field]
    normalized["paper_metadata"] = metadata
    normalized = normalize_ai_summary(normalized)
    validate_canonical_ai_summary(
        normalized,
        label="Kalyanaram supplemental ai_summary",
    )
    return normalized


def materialize_summary(
    draft_record: Mapping[str, Any],
    work_item: PaperWorkItem,
    evidence: EvidencePaths,
    *,
    zotero_parent_key: str,
    zotero_attachment_key: str,
) -> dict[str, Any]:
    work_item.validate()
    if work_item.canonical_paper_key != TARGET_DOI:
        raise KalyanaramCanonicalError(
            "runtime canonical_paper_key does not match the expected bare DOI"
        )
    if work_item.source_paper_id != TARGET_DOI:
        raise KalyanaramCanonicalError(
            "runtime source_paper_id does not match the expected bare DOI"
        )

    source_pdf = Path(work_item.source_pdf).expanduser().resolve()
    if not source_pdf.is_file():
        raise KalyanaramCanonicalError(f"source PDF does not exist: {source_pdf}")
    source_hash = file_sha256(source_pdf)
    descriptor = deepcopy(work_item.source_descriptor)
    descriptor_hash = str(descriptor.get("source_pdf_fingerprint") or "").lower()
    if descriptor_hash and descriptor_hash != source_hash:
        raise KalyanaramCanonicalError(
            "runtime source descriptor fingerprint does not match the source PDF"
        )

    draft = deepcopy(dict(draft_record))
    draft_paper = dict(draft.get("paper_info") or {})
    runtime_paper = deepcopy(work_item.paper_info)
    paper_info = {**draft_paper, **runtime_paper}
    aliases = _dedupe_strings(
        [
            *(runtime_paper.get("paper_key_aliases") or []),
            *(descriptor.get("paper_key_aliases") or []),
            *(draft_paper.get("paper_key_aliases") or []),
            TARGET_DOI,
            f"doi:{TARGET_DOI}",
            zotero_parent_key,
        ]
    )
    paper_info.update(
        {
            "source_mode": work_item.source_mode,
            "source_paper_id": work_item.source_paper_id,
            "canonical_paper_key": work_item.canonical_paper_key,
            "paper_key_aliases": aliases,
            "source_pdf": str(source_pdf),
            "pdf_path": str(source_pdf),
            "source_pdf_fingerprint": source_hash,
            "source_descriptor": descriptor,
            "zotero_parent_key": zotero_parent_key,
            "zotero_attachment_key": zotero_attachment_key,
        }
    )

    markdown_text = evidence.markdown_path.read_text(encoding="utf-8")
    chunks = json.loads(evidence.chunks_path.read_text(encoding="utf-8"))
    page_index = json.loads(evidence.page_index_path.read_text(encoding="utf-8"))
    if not isinstance(chunks, list) or len(chunks) != EXPECTED_PAGE_COUNT:
        raise KalyanaramCanonicalError("chunks evidence does not contain ten pages")
    if not isinstance(page_index, list) or len(page_index) != EXPECTED_PAGE_COUNT:
        raise KalyanaramCanonicalError("page-index evidence does not contain ten pages")
    completeness = build_completeness_metrics(
        text=markdown_text,
        page_count=EXPECTED_PAGE_COUNT,
        candidate_lengths={"tesseract_ocr": len(markdown_text)},
        chunk_count=len(chunks),
    )
    blocking_reasons = list(completeness.get("blocking_reasons") or [])
    quality_reasons = _dedupe_strings(
        [
            *blocking_reasons,
            *(completeness.get("warning_reasons") or []),
        ]
    )

    preprocess = dict(draft.get("preprocess") or {})
    preprocess.update(
        {
            "used_ocr": True,
            "ocr_engine": "tesseract-5.5.0-eng-psm3",
            "analysis_input_kind": "ocr_page_text_with_visual_readback",
            "markdown_path": str(evidence.markdown_path),
            "chunks_path": str(evidence.chunks_path),
            "page_index_path": str(evidence.page_index_path),
            "stage1_input_path": str(evidence.markdown_path),
            "selected_text_source": "tesseract_ocr",
            "selected_text_length": len(markdown_text),
            "stage1_page_count": EXPECTED_PAGE_COUNT,
            "page_count": EXPECTED_PAGE_COUNT,
            "stage1_quality_level": "BLOCK" if blocking_reasons else "PASS",
            "stage1_quality_reasons": quality_reasons,
            "stage1_completeness_metrics": completeness,
            "visual_readback_status": "ten_pages_verified",
        }
    )

    stage1_input = dict(draft.get("stage1_input") or {})
    stage1_input.update(
        {
            "input_mode": "text_ocr",
            "fallback_reason": "source_pdf_is_scanned_image",
            "source_mode": work_item.source_mode,
            "source_pdf": str(source_pdf),
            "source_pdf_fingerprint": source_hash,
            "canonical_paper_key": work_item.canonical_paper_key,
            "source_paper_id": work_item.source_paper_id,
            "zotero_parent_key": zotero_parent_key,
            "zotero_attachment_key": zotero_attachment_key,
            "selected_text_path": str(evidence.markdown_path),
            "selected_visual_refs": [],
            "provider_calls_used": 0,
        }
    )

    materialized = {
        **draft,
        "status": "success",
        "paper_info": paper_info,
        "ai_summary": _materialize_ai_summary(draft.get("ai_summary"), paper_info),
        "preprocess": preprocess,
        "stage1_input": stage1_input,
        "text_length": len(markdown_text),
        "processing_time": utc_now_iso(),
    }
    return materialized


def _load_single_draft(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KalyanaramCanonicalError(f"cannot read draft summary: {path}") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise KalyanaramCanonicalError(
            "supplemental Stage 1 draft must contain exactly one record"
        )
    record = payload[0]
    if not isinstance(record, dict):
        raise KalyanaramCanonicalError("supplemental Stage 1 draft record is invalid")
    return record


def run_canonical_stage1(
    *,
    source_pdf: str | Path,
    zotero_report: str | Path,
    draft_path: str | Path,
    ocr_dir: str | Path,
    evidence_dir: str | Path,
    project_name: str = PROJECT_NAME,
    config_path: str | Path = REPO_ROOT / "config.ini",
    queue_file: str | Path = REPO_ROOT / "output" / "_queue" / "queue.json",
    zotero_parent_key: str = ZOTERO_PARENT_KEY,
    zotero_attachment_key: str = ZOTERO_ATTACHMENT_KEY,
) -> dict[str, Any]:
    source = Path(source_pdf).expanduser().resolve()
    report = Path(zotero_report).expanduser().resolve()
    draft = Path(draft_path).expanduser().resolve()
    if not source.is_file():
        raise KalyanaramCanonicalError(f"source PDF does not exist: {source}")
    if file_sha256(source) != EXPECTED_SOURCE_SHA256:
        raise KalyanaramCanonicalError(
            "source PDF hash does not match the verified Zotero attachment"
        )
    if not report.is_file():
        raise KalyanaramCanonicalError(f"Zotero report does not exist: {report}")
    draft_record = _load_single_draft(draft)
    evidence = build_ocr_evidence(ocr_dir, evidence_dir)

    spec = RuntimeJobSpec(
        project_name=project_name,
        source=RuntimeSourceSpec(
            mode="zotero",
            zotero_report=str(report),
            library_path=str(source.parent),
        ),
        config=str(Path(config_path).expanduser().resolve()),
        action="analyze",
        queue_file=str(Path(queue_file).expanduser().resolve()),
        keep_legacy_projections=False,
        metadata={
            "requested_stages": ["analyze"],
            "validation_required": False,
            "require_clean_validation": False,
            "allow_unvalidated_when_validation_optional": True,
            "supplemental_stage1_contract": "kalyanaram_reference_price_bridge_v1",
        },
    )

    def stage_handler(stage_name: str, request: Any) -> dict[str, Any]:
        if stage_name != "stage1_analyze":
            raise KalyanaramCanonicalError(f"unexpected runtime stage: {stage_name}")
        items = request.source_bundle.paper_work_items
        if len(items) != 1:
            raise KalyanaramCanonicalError(
                f"Zotero intake must produce exactly one work item, got {len(items)}"
            )
        summary = materialize_summary(
            draft_record,
            items[0],
            evidence,
            zotero_parent_key=zotero_parent_key,
            zotero_attachment_key=zotero_attachment_key,
        )
        if summary["preprocess"]["stage1_quality_level"] != "PASS":
            raise KalyanaramCanonicalError(
                "OCR evidence failed the Stage 1 completeness gate: "
                f"{summary['preprocess']['stage1_quality_reasons']}"
            )
        return {
            "summaries": [summary],
            "source_items": [
                {
                    "path": str(draft),
                    "source_type": "verified_supplemental_stage1_draft",
                    "label": "kalyanaram_reference_price_bridge_v1",
                    "priority": 0,
                    "content_hash": file_sha256(draft),
                }
            ],
            "rejected_candidates": [],
            "model_call_count": 0,
            "subagent_run_id": "local-verified-supplemental-stage1",
        }

    import main as legacy_main

    result = AgentRuntimeRunner(
        spec,
        legacy_main=legacy_main,
        stage_handler=stage_handler,
    ).run()
    reconcile = AgentRuntimeRunner.reconcile(result.workspace_path)
    required_stages = {"source_intake", "analyze"}
    if result.job_status != "completed" or not result.canonical_ready:
        raise KalyanaramCanonicalError(
            "runtime did not produce a completed canonical Stage 1 job: "
            f"status={result.job_status}, disposition={result.job_disposition}"
        )
    if not required_stages.issubset(result.completed_stages):
        raise KalyanaramCanonicalError(
            f"runtime completed stages are incomplete: {result.completed_stages}"
        )
    if not reconcile.clean or not required_stages.issubset(reconcile.completed_stages):
        raise KalyanaramCanonicalError(
            "runtime reconcile did not prove a clean Stage 1 job: "
            f"issues={[issue.code for issue in reconcile.issues]}"
        )
    return {
        "job_id": result.job_id,
        "workspace_path": result.workspace_path,
        "job_status": result.job_status,
        "job_disposition": result.job_disposition,
        "canonical_ready": result.canonical_ready,
        "completed_stages": list(result.completed_stages),
        "reconcile_completed_stages": list(reconcile.completed_stages),
        "reconcile_issues": [issue.code for issue in reconcile.issues],
        "model_call_count": 0,
        "source_pdf_sha256": file_sha256(source),
        "evidence": evidence.to_dict(),
    }


def audit_workspace(workspace_path: str | Path) -> dict[str, Any]:
    reconcile = AgentRuntimeRunner.reconcile(Path(workspace_path).expanduser().resolve())
    required_stages = {"source_intake", "analyze"}
    return {
        "job_id": reconcile.job_id,
        "clean": reconcile.clean,
        "required_stages_complete": required_stages.issubset(
            reconcile.completed_stages
        ),
        "completed_stages": list(reconcile.completed_stages),
        "repaired_artifact_ids": list(reconcile.repaired_artifact_ids),
        "reconstructed_stage_records": list(
            reconcile.reconstructed_stage_records
        ),
        "outcome_repaired": reconcile.outcome_repaired,
        "pointer_repaired": reconcile.pointer_repaired,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "artifact_id": issue.artifact_id,
                "stage_name": issue.stage_name,
            }
            for issue in reconcile.issues
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Canonical Kalyanaram supplemental Stage 1 materializer."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    evidence_parser = subparsers.add_parser("build-evidence")
    evidence_parser.add_argument("--ocr-dir", type=Path, default=DEFAULT_OCR_DIR)
    evidence_parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
    )

    run_parser = subparsers.add_parser("run-stage1")
    run_parser.add_argument("--source-pdf", type=Path, default=DEFAULT_SOURCE_PDF)
    run_parser.add_argument(
        "--zotero-report",
        type=Path,
        default=DEFAULT_SOURCE_REPORT,
    )
    run_parser.add_argument("--draft", type=Path, default=DEFAULT_DRAFT)
    run_parser.add_argument("--ocr-dir", type=Path, default=DEFAULT_OCR_DIR)
    run_parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
    )
    run_parser.add_argument("--project-name", default=PROJECT_NAME)
    run_parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.ini")
    run_parser.add_argument(
        "--queue-file",
        type=Path,
        default=REPO_ROOT / "output" / "_queue" / "queue.json",
    )
    run_parser.add_argument("--zotero-parent-key", default=ZOTERO_PARENT_KEY)
    run_parser.add_argument(
        "--zotero-attachment-key",
        default=ZOTERO_ATTACHMENT_KEY,
    )

    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build-evidence":
            payload: Mapping[str, Any] = build_ocr_evidence(
                args.ocr_dir,
                args.evidence_dir,
            ).to_dict()
        elif args.command == "run-stage1":
            payload = run_canonical_stage1(
                source_pdf=args.source_pdf,
                zotero_report=args.zotero_report,
                draft_path=args.draft,
                ocr_dir=args.ocr_dir,
                evidence_dir=args.evidence_dir,
                project_name=args.project_name,
                config_path=args.config,
                queue_file=args.queue_file,
                zotero_parent_key=args.zotero_parent_key,
                zotero_attachment_key=args.zotero_attachment_key,
            )
        else:
            payload = audit_workspace(args.workspace)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (KalyanaramCanonicalError, RuntimeRunnerError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
