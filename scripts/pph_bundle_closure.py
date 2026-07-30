from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "output"
DEFAULT_BUNDLE_DIR = OUTPUT_ROOT / "pph_review_bundle_final"
DEFAULT_ACCEPTANCE_ROOT = Path(
    r"C:\Users\12130\Desktop\新建文件夹\博good good study\促销与使用意愿"
) / "literature_rebuild_20260727" / "acceptance_closure_20260728"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from docx_writer import (  # noqa: E402
    generate_apa_references_from_manifest,
    render_structured_citations,
    scan_docx_for_unresolved_citation_tokens,
)
from services.artifact_registry import (  # noqa: E402
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from validation.input_dependencies import (  # noqa: E402
    validate_validation_dependency_contract,
)
from validation.run_result import ValidationRunResultV1  # noqa: E402


EXPECTED_ACCEPTANCE_COUNTS = {
    "registry_rows": 88,
    "parents": 88,
    "membership_pairs": 145,
    "eligible": 84,
    "citation_ready": 84,
    "excluded": 4,
    "do_not_cite": 4,
    "claim_rows": 19,
    "evidence_rows": 108,
    "trace_gap_rows": 6,
    "pdf_attachments": 112,
    "parents_without_pdf": 0,
}
CURRENT_EXCLUDED_KEYS = frozenset(
    {"F99AI44H", "Q7QAXKGH", "UF638ICN", "US9R72ZQ"}
)
STALE_EXCLUDED_KEY = "XV3MYV2A"
KEYLESS_THEORETICAL_CLAIMS = frozenset({"C03-04", "C05-03"})

TOPICS: dict[str, dict[str, Any]] = {
    "S01": {
        "project_name": "pph_s01_dynamic_disadvantage",
        "job_id": "20260728_054303_5ab4252e",
        "expected_sections": 7,
        "bundle_stem": "01_dynamic_pricing_and_disadvantage_review",
        "title": "动态定价、人际价格劣势与消费者反应",
    },
    "S02": {
        "project_name": "pph_s02_prior_concession",
        "job_id": "20260728_063103_df0fe480",
        "expected_sections": 5,
        "bundle_stem": "02_platform_prior_concession_review",
        "title": "平台既往让利、补贴与消费者反应",
    },
    "S03": {
        "project_name": "pph_s03_concession_to_unfairness",
        "job_id": "20260728_063453_5344a69b",
        "expected_sections": 5,
        "bundle_stem": "03_prior_concession_to_unfairness_review",
        "title": "平台既往让利到价格不公平感的理论桥梁",
    },
    "S04": {
        "project_name": "pph_s04_unfairness_continuance",
        "job_id": "20260728_063507_60155d3b",
        "expected_sections": 10,
        "bundle_stem": "04_unfairness_to_continuance_review",
        "title": "价格不公平感与持续使用意愿及相邻结果变量",
    },
    "S05": {
        "project_name": "pph_s05_subjective_knowledge",
        "job_id": "20260728_063507_e48eec64",
        "expected_sections": 13,
        "bundle_stem": "05_subjective_knowledge_moderation_review",
        "title": "商业模式主观知识的调节作用",
    },
}


class BundleClosureError(RuntimeError):
    """Raised when the final bundle cannot be proven from canonical inputs."""


@dataclass(frozen=True)
class TopicClosure:
    project_id: str
    config: Mapping[str, Any]
    workspace: Path
    registry_path: Path
    draft_record: ArtifactRecord
    manifest_record: ArtifactRecord
    docx_record: ArtifactRecord
    validation_record: ArtifactRecord
    draft: Mapping[str, Any]
    manifest: Mapping[str, Any]
    validation: ValidationRunResultV1
    docx_scan: Mapping[str, Any]


@dataclass(frozen=True)
class AcceptanceClosure:
    root: Path
    manifest: Mapping[str, Any]
    eligibility_rows: tuple[dict[str, str], ...]
    readiness_rows: tuple[dict[str, str], ...]
    claim_rows: tuple[dict[str, str], ...]
    exact_set_audit: Mapping[str, Any]
    evidence_coverage_audit: Mapping[str, Any]
    claim_map_audit: Mapping[str, Any]


class _RenderContext:
    summaries: list[Any] = []
    logger = logging.getLogger("pph_bundle_closure")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleClosureError(f"cannot load JSON {path}: {exc}") from exc


def _read_csv(path: Path) -> tuple[dict[str, str], ...]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return tuple(dict(row) for row in csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BundleClosureError(f"cannot load CSV {path}: {exc}") from exc


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    path.write_text(normalized, encoding="utf-8", newline="\n")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    fieldnames: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(fieldnames),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _workspace_path(config: Mapping[str, Any], *, output_root: Path = OUTPUT_ROOT) -> Path:
    return output_root / f"{config['project_name']}__{config['job_id']}"


def _canonical_docx_path(workspace: Path, config: Mapping[str, Any]) -> Path:
    return workspace / "reports" / f"{config['project_name']}_literature_review.docx"


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _require_ready_record(record: ArtifactRecord | None, *, label: str) -> ArtifactRecord:
    if record is None:
        raise BundleClosureError(f"{label} is not registered")
    if record.status != "ready":
        raise BundleClosureError(f"{label} is not ready: {record.status}")
    path = Path(record.path)
    if not path.is_file():
        raise BundleClosureError(f"{label} file is missing: {path}")
    if not record.content_hash or file_sha256(path) != record.content_hash:
        raise BundleClosureError(f"{label} registry/disk hash mismatch: {path}")
    return record


def _dependency_identity(record: ArtifactRecord) -> tuple[str, str, str]:
    return (record.artifact_type, record.artifact_id, record.content_hash)


def _require_exact_dependencies(
    record: ArtifactRecord,
    expected: Iterable[ArtifactRecord],
    *,
    label: str,
) -> None:
    actual = Counter(
        (item.artifact_type, item.artifact_id, item.content_hash)
        for item in record.depends_on
    )
    required = Counter(_dependency_identity(item) for item in expected)
    if actual != required:
        raise BundleClosureError(f"{label} dependencies do not exactly match canonical inputs")


def _parse_time(value: str, *, label: str) -> datetime:
    normalized = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BundleClosureError(f"invalid {label} timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _select_current_validation(
    registry: ArtifactRegistry,
    records: Sequence[ArtifactRecord],
    *,
    draft_record: ArtifactRecord,
    manifest_record: ArtifactRecord,
) -> tuple[ArtifactRecord, ValidationRunResultV1]:
    current: list[tuple[ArtifactRecord, ValidationRunResultV1]] = []
    for record in records:
        if record.artifact_type != "validation_run_result" or record.status != "ready":
            continue
        path = Path(record.path)
        if not path.is_file() or not record.content_hash:
            continue
        if file_sha256(path) != record.content_hash:
            continue
        try:
            payload = _load_json(path)
            result = ValidationRunResultV1.from_dict(payload)
            result.validate()
        except (BundleClosureError, TypeError, ValueError) as exc:
            raise BundleClosureError(
                f"current validation payload is invalid for {record.artifact_id}: {exc}"
            ) from exc
        current.append((record, result))

    if not current:
        raise BundleClosureError("no current hash-matching validation_run_result is ready")
    current.sort(key=lambda item: (_parse_time(item[0].created_at, label="validation"), item[0].artifact_id))
    record, result = current[-1]

    if result.validation_run_id != record.artifact_id:
        raise BundleClosureError("validation payload id does not match Registry artifact id")
    if result.job_id != registry.job_id:
        raise BundleClosureError("validation payload job_id does not match Registry owner")
    if result.execution_status.value != "succeeded":
        raise BundleClosureError(f"latest current validation did not succeed: {result.execution_status.value}")
    if result.validation_disposition.value != "clean":
        raise BundleClosureError(
            f"latest current validation is not clean: {result.validation_disposition.value}"
        )
    if result.compatibility_status != "verified":
        raise BundleClosureError("latest current validation is not a verified canonical result")
    if result.expected_claim_count != result.validated_claim_count:
        raise BundleClosureError("validation claim counts are incomplete")
    if result.validated_claim_count <= 0:
        raise BundleClosureError("validation result contains no validated claims")
    if not result.evidence_complete:
        raise BundleClosureError("validation evidence closure is incomplete")
    if any(item.verdict.value != "supported" for item in result.claim_results):
        raise BundleClosureError("clean validation contains a non-supported claim verdict")

    inputs = result.input_artifacts
    if (
        inputs.review_draft_id != draft_record.artifact_id
        or inputs.review_draft_hash != draft_record.content_hash
        or inputs.citation_manifest_id != manifest_record.artifact_id
        or inputs.citation_manifest_hash != manifest_record.content_hash
    ):
        raise BundleClosureError("validation primary inputs are not the current canonical draft/manifest")

    try:
        validate_validation_dependency_contract(record, inputs)
        registry.verify_ready_dependencies(record.depends_on)
    except (OSError, TypeError, ValueError) as exc:
        raise BundleClosureError(f"validation dependency closure is invalid: {exc}") from exc

    validation_time = _parse_time(record.created_at, label="validation")
    source_time = max(
        _parse_time(draft_record.created_at, label="review draft"),
        _parse_time(manifest_record.created_at, label="citation manifest"),
    )
    if validation_time < source_time:
        raise BundleClosureError("validation predates its current canonical inputs")
    return record, result


def _scan_rendered_text(text: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    unresolved = sorted(
        set(
            re.findall(r"\[\[cite_ref:[^\]]+\]\]", text)
            + re.findall(r"\[\[cite:[^\]]+\]\]", text)
        )
    )
    ref_ids = {
        str(item.get("ref_id") or "").strip()
        for item in manifest.get("occurrences", [])
        if isinstance(item, Mapping) and str(item.get("ref_id") or "").strip()
    }
    bare_ref_ids = sorted(
        ref_id
        for ref_id in ref_ids
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ref_id)}(?![A-Za-z0-9_])", text)
    )
    return {
        "unresolved_tokens": unresolved,
        "bare_ref_ids": bare_ref_ids,
        "passed": not unresolved and not bare_ref_ids,
    }


def render_review_markdown(
    title: str,
    review_draft: Mapping[str, Any],
    citation_manifest: Mapping[str, Any],
) -> str:
    if review_draft.get("artifact_type") != "review_draft" or review_draft.get("artifact_version") != "v2":
        raise BundleClosureError("Markdown projection requires review_draft v2")
    if citation_manifest.get("artifact_type") != "citation_manifest" or citation_manifest.get("artifact_version") != "v3":
        raise BundleClosureError("Markdown projection requires citation_manifest v3")

    sections = (review_draft.get("content") or {}).get("sections") or []
    if not isinstance(sections, list) or not sections:
        raise BundleClosureError("review draft has no sections")

    lines = [f"# {title}", ""]
    for section in sections:
        if not isinstance(section, Mapping):
            raise BundleClosureError("review draft section is not an object")
        number = int(section.get("section_number") or 0)
        section_title = str(section.get("section_title") or "").strip()
        if number <= 0 or not section_title:
            raise BundleClosureError("review draft section identity is incomplete")
        lines.extend([f"## {number}. {section_title}", ""])
        blocks = section.get("blocks") or []
        if not isinstance(blocks, list) or not blocks:
            raise BundleClosureError(f"review draft section {number} has no blocks")
        for block in blocks:
            if not isinstance(block, Mapping):
                raise BundleClosureError(f"review draft section {number} has an invalid block")
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            rendered, unresolved = render_structured_citations(
                text,
                _RenderContext(),
                citation_manifest,
                allow_compat_fallback=False,
            )
            if unresolved:
                raise BundleClosureError(
                    f"section {number} has unresolved citation identities: {sorted(set(unresolved))}"
                )
            lines.extend([rendered.strip(), ""])

    references = generate_apa_references_from_manifest(
        dict(citation_manifest),
        _RenderContext(),
        allow_compat_fallback=False,
    )
    if not references:
        raise BundleClosureError("canonical citation manifest produced no bibliography")
    lines.extend(["## References", ""])
    lines.extend(f"- {reference}" for reference in references)
    rendered_markdown = "\n".join(lines).rstrip() + "\n"
    scan = _scan_rendered_text(rendered_markdown, citation_manifest)
    if not scan["passed"]:
        raise BundleClosureError(f"rendered Markdown citation scan failed: {scan}")
    return rendered_markdown


def load_topic_closure(
    project_id: str,
    *,
    output_root: Path = OUTPUT_ROOT,
) -> TopicClosure:
    config = TOPICS[project_id]
    workspace = _workspace_path(config, output_root=output_root)
    registry_path = workspace / "artifact_registry.json"
    if not registry_path.is_file():
        raise BundleClosureError(f"{project_id} Registry is missing: {registry_path}")
    registry = ArtifactRegistry(registry_path, str(config["job_id"]))
    registry.reload()
    records = registry.list_records()

    draft_record = _require_ready_record(
        registry.get("review_draft_v2:full_review"),
        label=f"{project_id} review_draft_v2:full_review",
    )
    if draft_record.artifact_type != "review_draft" or draft_record.artifact_version != "v2":
        raise BundleClosureError(f"{project_id} canonical review draft is not v2")
    manifest_record = _require_ready_record(
        registry.get("citation_manifest:v3"),
        label=f"{project_id} citation_manifest:v3",
    )
    if manifest_record.artifact_type != "citation_manifest" or manifest_record.artifact_version != "v3":
        raise BundleClosureError(f"{project_id} canonical citation manifest is not v3")

    canonical_docx = _canonical_docx_path(workspace, config)
    docx_candidates = [
        record
        for record in records
        if record.artifact_type == "review_docx"
        and _normalized_path(record.path) == _normalized_path(canonical_docx)
    ]
    if len(docx_candidates) != 1:
        raise BundleClosureError(
            f"{project_id} requires exactly one registered canonical review_docx; found {len(docx_candidates)}"
        )
    docx_record = _require_ready_record(docx_candidates[0], label=f"{project_id} review_docx")
    try:
        registry.verify_ready_dependencies(docx_record.depends_on)
    except (OSError, TypeError, ValueError) as exc:
        raise BundleClosureError(f"{project_id} DOCX dependencies are invalid: {exc}") from exc
    _require_exact_dependencies(
        docx_record,
        [draft_record, manifest_record],
        label=f"{project_id} review_docx",
    )

    draft = _load_json(Path(draft_record.path))
    manifest = _load_json(Path(manifest_record.path))
    if draft.get("created_from_job_id") != config["job_id"]:
        raise BundleClosureError(f"{project_id} draft owner mismatch")
    if manifest.get("created_from_job_id") != config["job_id"]:
        raise BundleClosureError(f"{project_id} manifest owner mismatch")
    sections = (draft.get("content") or {}).get("sections") or []
    actual_sections = [int(item.get("section_number") or 0) for item in sections]
    expected_sections = list(range(1, int(config["expected_sections"]) + 1))
    if actual_sections != expected_sections:
        raise BundleClosureError(
            f"{project_id} section closure mismatch: expected {expected_sections}, got {actual_sections}"
        )

    validation_record, validation = _select_current_validation(
        registry,
        records,
        draft_record=draft_record,
        manifest_record=manifest_record,
    )
    docx_scan = scan_docx_for_unresolved_citation_tokens(docx_record.path, manifest)
    if not docx_scan.get("passed") or not docx_scan.get("references_seen"):
        raise BundleClosureError(f"{project_id} canonical DOCX citation scan failed: {docx_scan}")

    render_review_markdown(str(config["title"]), draft, manifest)
    return TopicClosure(
        project_id=project_id,
        config=config,
        workspace=workspace,
        registry_path=registry_path,
        draft_record=draft_record,
        manifest_record=manifest_record,
        docx_record=docx_record,
        validation_record=validation_record,
        draft=draft,
        manifest=manifest,
        validation=validation,
        docx_scan=docx_scan,
    )


def _verify_manifest_artifacts(root: Path, manifest: Mapping[str, Any]) -> None:
    entries = manifest.get("artifacts") or []
    if not isinstance(entries, list) or not entries:
        raise BundleClosureError("acceptance manifest has no artifact identities")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise BundleClosureError("acceptance artifact identity is invalid")
        relative = str(entry.get("relative_path") or "").strip()
        path = root / relative
        if not relative or not path.is_file():
            raise BundleClosureError(f"acceptance artifact is missing: {relative}")
        if path.stat().st_size != int(entry.get("size_bytes") or -1):
            raise BundleClosureError(f"acceptance artifact size mismatch: {relative}")
        if file_sha256(path) != str(entry.get("sha256") or ""):
            raise BundleClosureError(f"acceptance artifact hash mismatch: {relative}")
        if path.suffix.lower() == ".csv":
            actual_count = len(_read_csv(path))
            if actual_count != int(entry.get("record_count") or 0):
                raise BundleClosureError(f"acceptance CSV row count mismatch: {relative}")
        elif path.suffix.lower() == ".jsonl":
            actual_count = sum(
                1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
            if actual_count != int(entry.get("record_count") or 0):
                raise BundleClosureError(f"acceptance JSONL row count mismatch: {relative}")


def _verify_detached_hash(
    manifest_path: Path,
    detached_path: Path,
    *,
    label: str,
) -> None:
    try:
        detached_parts = detached_path.read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeError) as exc:
        raise BundleClosureError(f"cannot read {label} detached hash: {detached_path}") from exc
    if len(detached_parts) != 2 or detached_parts[1] != manifest_path.name:
        raise BundleClosureError(f"{label} detached hash has invalid format")
    expected_hash = detached_parts[0]
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise BundleClosureError(f"{label} detached hash is not lowercase SHA-256")
    if not manifest_path.is_file() or expected_hash != file_sha256(manifest_path):
        raise BundleClosureError(f"{label} detached hash mismatch")


def _split_keys(value: Any) -> tuple[str, ...]:
    return tuple(
        key
        for key in (item.strip() for item in str(value or "").split(";"))
        if key
    )


def load_acceptance_closure(root: Path = DEFAULT_ACCEPTANCE_ROOT) -> AcceptanceClosure:
    root = root.resolve()
    manifest_path = root / "15_final_closure_manifest.json"
    _verify_detached_hash(
        manifest_path,
        root / "16_final_closure_manifest.sha256",
        label="acceptance closure manifest",
    )
    manifest = _load_json(manifest_path)
    counts = manifest.get("acceptance_counts") or {}
    for name, expected in EXPECTED_ACCEPTANCE_COUNTS.items():
        if int(counts.get(name, -1)) != expected:
            raise BundleClosureError(
                f"acceptance count mismatch for {name}: expected {expected}, got {counts.get(name)}"
            )
    if set(manifest.get("excluded_keys") or []) != set(CURRENT_EXCLUDED_KEYS):
        raise BundleClosureError("acceptance excluded-key set is not canonical")
    if manifest.get("stale_extra_key_removed") != STALE_EXCLUDED_KEY:
        raise BundleClosureError("acceptance stale exclusion identity is missing")
    zotero_contract = manifest.get("zotero_contract") or {}
    if (
        zotero_contract.get("mode") != "read-only"
        or int(zotero_contract.get("write_operations", -1)) != 0
        or set(zotero_contract.get("observed_http_methods") or []) != {"GET"}
    ):
        raise BundleClosureError("acceptance Zotero read-only contract is not closed")
    _verify_manifest_artifacts(root, manifest)

    eligibility_rows = _read_csv(root / "05_eligibility_manifest.csv")
    readiness_rows = _read_csv(root / "06_citation_readiness.csv")
    claim_rows = _read_csv(root / "11_claim_citation_map.csv")
    exact_set_audit = _load_json(root / "13_exact_set_audit.json")
    evidence_coverage_audit = _load_json(root / "10_evidence_coverage_audit.json")
    claim_map_audit = _load_json(root / "12_claim_map_audit.json")
    if exact_set_audit.get("all_exact_set_assertions_passed") is not True:
        raise BundleClosureError("acceptance exact-set audit did not pass")
    if evidence_coverage_audit.get("exact_live_key_set_match") is not True:
        raise BundleClosureError("acceptance evidence key set is not exact")
    if (
        claim_map_audit.get("all_real_keys_formally_eligible") is not True
        or claim_map_audit.get("all_real_keys_live") is not True
    ):
        raise BundleClosureError("acceptance claim-map key closure did not pass")

    if len(eligibility_rows) != 88 or len(readiness_rows) != 88 or len(claim_rows) != 19:
        raise BundleClosureError("acceptance canonical table counts are inconsistent")
    eligibility_by_key = {row["zotero_key"]: row for row in eligibility_rows}
    readiness_by_key = {row["zotero_key"]: row for row in readiness_rows}
    excluded = {
        key for key, row in readiness_by_key.items() if row.get("status") == "DO_NOT_CITE"
    }
    if excluded != set(CURRENT_EXCLUDED_KEYS):
        raise BundleClosureError("citation-readiness excluded-key set is inconsistent")
    if STALE_EXCLUDED_KEY in eligibility_by_key or STALE_EXCLUDED_KEY in readiness_by_key:
        raise BundleClosureError("stale non-SSCI source reappeared in the current closure")

    observed_keyless: set[str] = set()
    for row in claim_rows:
        claim_id = row.get("claim_id", "")
        keys = _split_keys(row.get("zotero_keys"))
        if not keys:
            observed_keyless.add(claim_id)
        for key in keys:
            readiness = readiness_by_key.get(key)
            eligibility = eligibility_by_key.get(key)
            if readiness is None or eligibility is None:
                raise BundleClosureError(f"claim {claim_id} references a non-live key: {key}")
            if readiness.get("status") != "CITATION_READY" or eligibility.get("eligibility") != "eligible":
                raise BundleClosureError(f"claim {claim_id} references a non-formal key: {key}")
    if observed_keyless != set(KEYLESS_THEORETICAL_CLAIMS):
        raise BundleClosureError("keyless theoretical claim set is inconsistent")

    claim_by_id = {row["claim_id"]: row for row in claim_rows}
    if "不得把购买复购忠诚统一改名为持续使用意愿" not in claim_by_id["C04-01"].get("wording_limit", ""):
        raise BundleClosureError("C04-01 outcome-variable boundary is missing")
    if "严格保留原结果变量名不得改名" not in claim_by_id["C04-03"].get("wording_limit", ""):
        raise BundleClosureError("C04-03 outcome-variable boundary is missing")

    return AcceptanceClosure(
        root=root,
        manifest=manifest,
        eligibility_rows=eligibility_rows,
        readiness_rows=readiness_rows,
        claim_rows=claim_rows,
        exact_set_audit=exact_set_audit,
        evidence_coverage_audit=evidence_coverage_audit,
        claim_map_audit=claim_map_audit,
    )


def build_argument_evidence_rows(acceptance: AcceptanceClosure) -> list[dict[str, Any]]:
    eligibility = {row["zotero_key"]: row for row in acceptance.eligibility_rows}
    readiness = {row["zotero_key"]: row for row in acceptance.readiness_rows}
    output: list[dict[str, Any]] = []
    for row in acceptance.claim_rows:
        keys = _split_keys(row.get("zotero_keys"))
        source_rows = [readiness[key] for key in keys]
        eligibility_rows = [eligibility[key] for key in keys]
        projected: dict[str, Any] = dict(row)
        projected.update(
            {
                "formal_source_count": len(keys),
                "formal_source_titles": " | ".join(item.get("title", "") for item in source_rows),
                "formal_source_dois": " | ".join(item.get("doi", "") for item in eligibility_rows),
                "formal_source_index_systems": " | ".join(
                    item.get("index_system", "") for item in source_rows
                ),
                "all_sources_citation_ready": str(
                    bool(keys)
                    and all(item.get("status") == "CITATION_READY" for item in source_rows)
                ).lower(),
                "all_sources_pdf_available": str(
                    bool(keys)
                    and all(item.get("has_pdf", "").lower() == "true" for item in source_rows)
                ).lower(),
                "source_scope": (
                    "formal_claim_evidence"
                    if keys
                    else "keyless_theoretical_proposition_or_hypothesis"
                ),
            }
        )
        output.append(projected)
    return output


def build_exclusion_rows(acceptance: AcceptanceClosure) -> list[dict[str, Any]]:
    eligibility = {row["zotero_key"]: row for row in acceptance.eligibility_rows}
    readiness = {row["zotero_key"]: row for row in acceptance.readiness_rows}
    rows: list[dict[str, Any]] = []
    for key in sorted(CURRENT_EXCLUDED_KEYS):
        eligible = eligibility[key]
        ready = readiness[key]
        rows.append(
            {
                "zotero_key": key,
                "title": eligible.get("title", ""),
                "doi": eligible.get("doi", ""),
                "journal": ready.get("journal", ""),
                "index_system": eligible.get("index_system", ""),
                "status": ready.get("status", ""),
                "exclusion_reason": eligible.get("exclusion_reason", ""),
                "exclusion_scope": "current_control_snapshot",
                "present_in_current_closure": "true",
                "has_pdf": eligible.get("has_pdf", ""),
                "live_readback_verified": eligible.get("live_readback_verified", ""),
                "control_high_water_version": eligible.get("control_high_water_version", ""),
            }
        )
    rows.append(
        {
            "zotero_key": STALE_EXCLUDED_KEY,
            "title": "From Free-to-Fee: Motive-Based Communication and Price Fairness for Value-Added Services",
            "doi": "10.5771/2511-8676-2025-3-4-118",
            "journal": "Journal of Service Management Research",
            "index_system": "NOT-SSCI",
            "status": "DO_NOT_CITE",
            "exclusion_reason": "非SSCI；已从当前正式集合、claim map 和 citation readiness exact set 中移除",
            "exclusion_scope": "stale_source_tombstone",
            "present_in_current_closure": "false",
            "has_pdf": "not_applicable_to_current_closure",
            "live_readback_verified": "removed_before_current_closure",
            "control_high_water_version": str(
                (acceptance.manifest.get("control_snapshot") or {}).get("high_water_version", "")
            ),
        }
    )
    return rows


def build_integrated_synthesis(acceptance: AcceptanceClosure) -> str:
    rows_by_section: dict[str, list[dict[str, str]]] = {}
    for row in acceptance.claim_rows:
        rows_by_section.setdefault(row.get("section", ""), []).append(row)
    evidence_roles = Counter(row.get("direct_or_bridge", "") or "unspecified" for row in acceptance.claim_rows)
    lines = [
        "# 平台既往让利、价格不公平感与持续使用意愿：整合性理论综合",
        "",
        "## 证据口径",
        "",
        (
            "本综合以冻结的 live Zotero closure 为边界：88 个父条目、84 个正式可引来源、"
            "4 个当前 DO_NOT_CITE 来源、19 条 claim-map 记录。所有 formal keys 均通过 live、"
            "期刊索引、PDF 和 citation-readiness 闭合检查。trace-gap 行仅提供元数据与 PDF 追踪，"
            "不用于推断理论、方法、结果或 claim support。"
        ),
        "",
        (
            "证据角色计数："
            + "；".join(f"{name}={count}" for name, count in sorted(evidence_roles.items()))
            + "。direct 表示最接近目标关系的证据；bridge 表示相邻机制桥梁；counter 表示反向证据；"
            "construct 表示构念或量表证据；n/a 及无 source key 的条目不得写成既有实证结论。"
        ),
        "",
    ]
    for section, rows in rows_by_section.items():
        lines.extend([f"## {section}", ""])
        for row in rows:
            keys = ", ".join(_split_keys(row.get("zotero_keys"))) or "无 formal source key"
            lines.extend(
                [
                    f"### {row.get('claim_id')}: {row.get('claim')}",
                    "",
                    f"- 证据类型：{row.get('evidence_type')} / {row.get('direct_or_bridge')}",
                    f"- Formal keys：{keys}",
                    f"- 写作边界：{row.get('wording_limit') or '按原研究构念与结果变量表述'}",
                    "",
                ]
            )
    lines.extend(
        [
            "## 跨主题整合结论",
            "",
            (
                "现有证据可支持一条分层而非端到端已证实的理论链：动态或人际价格劣势会触发比较、"
                "归因与公平判断；平台既往让利可能为后续定价提供动机归因和互惠判断的桥梁；"
                "价格不公平感又与购买、复购、忠诚、转换、投诉、留存或持续使用等不同结果变量相关。"
                "其中“既往让利直接缓释人际价格劣势下不公平感”的完整路径仍是待检验理论命题，"
                "不能由相邻桥梁证据改写成已有直接实证。"
            ),
            "",
            (
                "结果变量必须保持原研究名称和测量口径。purchase、repurchase、loyalty、switching、"
                "complaint、retention 与 continuance intention 彼此相关但不等价；移动应用中的持续使用"
                "证据也不能无条件外推到所有平台。"
            ),
            "",
            (
                "商业模式主观知识可作为消费者识别补贴、成本回收和平台获利逻辑的候选边界条件，"
                "但说服知识、互联网素养或一般市场知识不能自动等同于商业模式主观知识。"
                "该调节路径属于构念与机制桥接后的理论扩展，仍需直接实证。"
            ),
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _topic_validation_index(topics: Sequence[TopicClosure]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for topic in topics:
        validation = topic.validation
        rows.append(
            {
                "project_id": topic.project_id,
                "project_name": topic.config["project_name"],
                "job_id": topic.config["job_id"],
                "workspace": str(topic.workspace),
                "review_draft_artifact_id": topic.draft_record.artifact_id,
                "review_draft_sha256": topic.draft_record.content_hash,
                "citation_manifest_artifact_id": topic.manifest_record.artifact_id,
                "citation_manifest_sha256": topic.manifest_record.content_hash,
                "review_docx_artifact_id": topic.docx_record.artifact_id,
                "review_docx_sha256": topic.docx_record.content_hash,
                "validation_artifact_id": topic.validation_record.artifact_id,
                "validation_sha256": topic.validation_record.content_hash,
                "validation_created_at": topic.validation_record.created_at,
                "execution_status": validation.execution_status.value,
                "validation_disposition": validation.validation_disposition.value,
                "expected_claim_count": validation.expected_claim_count,
                "validated_claim_count": validation.validated_claim_count,
                "supported_claim_count": validation.claim_verdict_counts.get("supported", 0),
                "evidence_manifest_count": len(validation.input_artifacts.evidence_manifest_ids),
                "dependency_count": len(topic.validation_record.depends_on),
                "docx_unresolved_token_count": len(topic.docx_scan.get("unresolved_tokens") or []),
                "docx_bare_ref_count": len(topic.docx_scan.get("bare_ref_ids") or []),
            }
        )
    return rows


def build_execution_report(
    acceptance: AcceptanceClosure,
    topics: Sequence[TopicClosure],
) -> str:
    counts = acceptance.manifest["acceptance_counts"]
    lines = [
        "# 执行与验证闭环报告",
        "",
        "## 最终状态",
        "",
        "- 五个专题均由当前 Registry 中的 review_draft_v2、citation_manifest_v3 和 review_docx 重建。",
        "- 五个专题最新且磁盘哈希匹配的 ValidationRunResultV1 均为 `succeeded / clean`。",
        "- 每个 validation 的 payload 输入集合与 Registry depends_on 通过精确多重集比较。",
        "- 旧 bundle 不作为内容或验证真值；替换时保留时间戳备份。",
        "",
        "## 文献与 Zotero closure",
        "",
        f"- Live parent items / registry rows：{counts['parents']} / {counts['registry_rows']}",
        f"- Collection membership pairs：{counts['membership_pairs']}",
        f"- Formal eligible / citation ready：{counts['eligible']} / {counts['citation_ready']}",
        f"- Current excluded / do-not-cite：{counts['excluded']} / {counts['do_not_cite']}",
        f"- Evidence rows / claim rows：{counts['evidence_rows']} / {counts['claim_rows']}",
        f"- PDF attachments / parents without PDF：{counts['pdf_attachments']} / {counts['parents_without_pdf']}",
        "- Zotero contract：read-only GET；write operations=0；control high-water=56570。",
        f"- Stale non-SSCI tombstone：{STALE_EXCLUDED_KEY}，不计入当前 88-row exact set。",
        "",
        "## 专题验证索引",
        "",
        "| Topic | Validation artifact | Claims | Draft SHA256 | Manifest SHA256 | DOCX SHA256 |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for topic in topics:
        lines.append(
            "| {project} | {validation} | {claims}/{claims} supported | `{draft}` | `{manifest}` | `{docx}` |".format(
                project=topic.project_id,
                validation=topic.validation_record.artifact_id,
                claims=topic.validation.validated_claim_count,
                draft=topic.draft_record.content_hash,
                manifest=topic.manifest_record.content_hash,
                docx=topic.docx_record.content_hash,
            )
        )
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "- direct、bridge、counter、construct 与 keyless theoretical proposition 在 07 文件中分开保留。",
            "- purchase、repurchase、loyalty、switching、complaint、retention、continuance intention 不作同义改写。",
            "- `F99AI44H`（SSRN working paper）及 `XV3MYV2A`（非 SSCI stale source）不得进入正式证据。",
            "- `Q7QAXKGH`、`UF638ICN`、`US9R72ZQ` 保留在当前 closure 的 DO_NOT_CITE 审计中，但不进入 formal claim keys。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _copy_canonical_topic_artifacts(staging: Path, topic: TopicClosure) -> None:
    canonical_dir = staging / "canonical_artifacts" / topic.project_id
    canonical_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(topic.registry_path, canonical_dir / "artifact_registry.json")
    shutil.copyfile(topic.draft_record.path, canonical_dir / "review_draft_v2.json")
    shutil.copyfile(topic.manifest_record.path, canonical_dir / "citation_manifest_v3.json")
    shutil.copyfile(topic.validation_record.path, canonical_dir / "validation_run_result_v1.json")


def _write_topic_outline(staging: Path, topic: TopicClosure) -> None:
    lines = [f"# {topic.config['title']}", ""]
    for section in (topic.draft.get("content") or {}).get("sections") or []:
        lines.append(f"- {section['section_number']}. {section['section_title']}")
    _write_text(staging / "outlines" / f"{topic.project_id}_canonical_outline.md", "\n".join(lines))


def _content_hash_rows(staging: Path) -> list[dict[str, Any]]:
    excluded = {
        "12_file_hash_audit.csv",
        "13_bundle_closure_manifest.json",
        "13_bundle_closure_manifest.sha256",
    }
    rows: list[dict[str, Any]] = []
    for path in sorted(item for item in staging.rglob("*") if item.is_file()):
        relative = path.relative_to(staging).as_posix()
        if relative in excluded:
            continue
        rows.append(
            {
                "relative_path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return rows


def _expected_bundle_paths() -> set[str]:
    paths = {
        "06_integrated_theoretical_synthesis.md",
        "07_argument_evidence_map.csv",
        "08_citation_readiness.csv",
        "09_excluded_literature.csv",
        "10_execution_report.md",
        "11_validation_artifact_index.csv",
        "12_file_hash_audit.csv",
        "13_bundle_closure_manifest.json",
        "13_bundle_closure_manifest.sha256",
        "eligibility_manifest.csv",
        "acceptance_closure_manifest.json",
    }
    for project_id, config in TOPICS.items():
        stem = str(config["bundle_stem"])
        paths.update(
            {
                f"{stem}.docx",
                f"{stem}.md",
                f"outlines/{project_id}_canonical_outline.md",
                f"canonical_artifacts/{project_id}/artifact_registry.json",
                f"canonical_artifacts/{project_id}/review_draft_v2.json",
                f"canonical_artifacts/{project_id}/citation_manifest_v3.json",
                f"canonical_artifacts/{project_id}/validation_run_result_v1.json",
            }
        )
    return paths


def _closure_source_time(topics: Sequence[TopicClosure]) -> str:
    latest = max(_parse_time(topic.validation_record.created_at, label="validation") for topic in topics)
    return latest.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_remove_staging(path: Path, *, output_root: Path) -> None:
    resolved = path.resolve()
    allowed = output_root.resolve()
    if resolved.parent != allowed or not resolved.name.startswith(".pph_review_bundle_final.staging-"):
        raise BundleClosureError(f"refusing to remove unexpected staging path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _safe_remove_final_bundle(path: Path, *, output_root: Path) -> None:
    resolved = path.resolve()
    allowed = output_root.resolve()
    if resolved.parent != allowed or resolved.name != "pph_review_bundle_final":
        raise BundleClosureError(f"refusing to remove unexpected final bundle path: {resolved}")
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.exists():
        resolved.unlink()


def _next_backup_path(output_root: Path) -> Path:
    backup_root = output_root / "_bundle_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = backup_root / f"pph_review_bundle_final__{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = backup_root / f"pph_review_bundle_final__{stamp}_{suffix}"
        suffix += 1
    return candidate


def _publish_staged_bundle(
    staging: Path,
    bundle_dir: Path,
    *,
    output_root: Path,
) -> dict[str, Any]:
    backup_path: Path | None = None
    try:
        if bundle_dir.exists():
            backup_path = _next_backup_path(output_root)
            shutil.move(str(bundle_dir), str(backup_path))
        shutil.move(str(staging), str(bundle_dir))
        final_audit = audit_built_bundle(bundle_dir)
    except Exception:
        if bundle_dir.exists():
            _safe_remove_final_bundle(bundle_dir, output_root=output_root)
        if backup_path is not None and backup_path.exists():
            shutil.move(str(backup_path), str(bundle_dir))
        raise
    final_audit["backup_path"] = str(backup_path) if backup_path else ""
    return final_audit


def audit_built_bundle(bundle_dir: Path) -> dict[str, Any]:
    bundle_dir = bundle_dir.resolve()
    manifest_path = bundle_dir / "13_bundle_closure_manifest.json"
    detached_path = bundle_dir / "13_bundle_closure_manifest.sha256"
    _verify_detached_hash(manifest_path, detached_path, label="bundle closure manifest")
    manifest = _load_json(manifest_path)
    audit_path = bundle_dir / "12_file_hash_audit.csv"
    if file_sha256(audit_path) != (manifest.get("file_hash_audit") or {}).get("sha256"):
        raise BundleClosureError("file-hash audit identity mismatch")
    rows = _read_csv(audit_path)
    for row in rows:
        path = bundle_dir / row["relative_path"]
        if not path.is_file():
            raise BundleClosureError(f"bundle file is missing: {row['relative_path']}")
        if path.stat().st_size != int(row["size_bytes"]):
            raise BundleClosureError(f"bundle file size mismatch: {row['relative_path']}")
        if file_sha256(path) != row["sha256"]:
            raise BundleClosureError(f"bundle file hash mismatch: {row['relative_path']}")

    metadata_paths = {
        "12_file_hash_audit.csv",
        "13_bundle_closure_manifest.json",
        "13_bundle_closure_manifest.sha256",
    }
    expected_paths = _expected_bundle_paths()
    listed_paths = {row["relative_path"] for row in rows}
    if listed_paths != expected_paths - metadata_paths:
        raise BundleClosureError("bundle file-hash audit does not list the canonical content path set")
    manifest_content_rows = manifest.get("content_files") or []
    manifest_identities = {
        (
            str(row.get("relative_path") or ""),
            int(row.get("size_bytes") or -1),
            str(row.get("sha256") or ""),
        )
        for row in manifest_content_rows
        if isinstance(row, Mapping)
    }
    audit_identities = {
        (row["relative_path"], int(row["size_bytes"]), row["sha256"])
        for row in rows
    }
    if manifest_identities != audit_identities or len(manifest_content_rows) != len(rows):
        raise BundleClosureError("bundle manifest content identities do not match the file-hash audit")
    actual_paths = {
        path.relative_to(bundle_dir).as_posix()
        for path in bundle_dir.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise BundleClosureError("bundle filesystem does not exactly match the canonical path set")
    if int(manifest.get("topic_count") or 0) != 5 or manifest.get("all_topics_clean") is not True:
        raise BundleClosureError("bundle closure manifest does not prove 5/5 clean")
    return {
        "bundle_dir": str(bundle_dir),
        "file_count": len(rows) + 3,
        "content_file_count": len(rows),
        "manifest_sha256": file_sha256(manifest_path),
        "all_topics_clean": True,
    }


def build_bundle(
    *,
    bundle_dir: Path = DEFAULT_BUNDLE_DIR,
    acceptance_root: Path = DEFAULT_ACCEPTANCE_ROOT,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, Any]:
    output_root = output_root.resolve()
    bundle_dir = bundle_dir.resolve()
    if bundle_dir.parent != output_root or bundle_dir.name != "pph_review_bundle_final":
        raise BundleClosureError(f"unexpected final bundle target: {bundle_dir}")

    acceptance = load_acceptance_closure(acceptance_root)
    topics = [load_topic_closure(project_id, output_root=output_root) for project_id in TOPICS]

    staging = output_root / f".pph_review_bundle_final.staging-{os.getpid()}"
    if staging.exists():
        _safe_remove_staging(staging, output_root=output_root)
    staging.mkdir(parents=False)
    try:
        for topic in topics:
            stem = str(topic.config["bundle_stem"])
            shutil.copyfile(topic.docx_record.path, staging / f"{stem}.docx")
            _write_text(
                staging / f"{stem}.md",
                render_review_markdown(str(topic.config["title"]), topic.draft, topic.manifest),
            )
            _copy_canonical_topic_artifacts(staging, topic)
            _write_topic_outline(staging, topic)

        _write_text(staging / "06_integrated_theoretical_synthesis.md", build_integrated_synthesis(acceptance))
        argument_rows = build_argument_evidence_rows(acceptance)
        argument_fields = [
            *acceptance.claim_rows[0].keys(),
            "formal_source_count",
            "formal_source_titles",
            "formal_source_dois",
            "formal_source_index_systems",
            "all_sources_citation_ready",
            "all_sources_pdf_available",
            "source_scope",
        ]
        _write_csv(staging / "07_argument_evidence_map.csv", argument_rows, argument_fields)
        shutil.copyfile(acceptance.root / "06_citation_readiness.csv", staging / "08_citation_readiness.csv")
        exclusion_fields = [
            "zotero_key",
            "title",
            "doi",
            "journal",
            "index_system",
            "status",
            "exclusion_reason",
            "exclusion_scope",
            "present_in_current_closure",
            "has_pdf",
            "live_readback_verified",
            "control_high_water_version",
        ]
        _write_csv(staging / "09_excluded_literature.csv", build_exclusion_rows(acceptance), exclusion_fields)
        _write_text(staging / "10_execution_report.md", build_execution_report(acceptance, topics))

        validation_rows = _topic_validation_index(topics)
        _write_csv(
            staging / "11_validation_artifact_index.csv",
            validation_rows,
            list(validation_rows[0].keys()),
        )
        shutil.copyfile(
            acceptance.root / "05_eligibility_manifest.csv",
            staging / "eligibility_manifest.csv",
        )
        shutil.copyfile(
            acceptance.root / "15_final_closure_manifest.json",
            staging / "acceptance_closure_manifest.json",
        )

        hash_rows = _content_hash_rows(staging)
        _write_csv(
            staging / "12_file_hash_audit.csv",
            hash_rows,
            ["relative_path", "size_bytes", "sha256"],
        )
        hash_audit_path = staging / "12_file_hash_audit.csv"
        closure_manifest = {
            "schema_version": 1,
            "closure_type": "pph_review_bundle_closure",
            "source_snapshot_at": _closure_source_time(topics),
            "topic_count": len(topics),
            "all_topics_clean": all(
                topic.validation.validation_disposition.value == "clean" for topic in topics
            ),
            "canonical_truth": {
                "review": "review_draft_v2:full_review + citation_manifest:v3",
                "validation": "fresh hash-matching ValidationRunResultV1 + exact Registry dependency multiset",
            },
            "acceptance_closure": {
                "closure_id": acceptance.manifest.get("closure_id"),
                "path": str(acceptance.root),
                "manifest_sha256": file_sha256(acceptance.root / "15_final_closure_manifest.json"),
                "acceptance_counts": acceptance.manifest.get("acceptance_counts"),
                "current_excluded_keys": sorted(CURRENT_EXCLUDED_KEYS),
                "stale_excluded_tombstone": STALE_EXCLUDED_KEY,
            },
            "topics": validation_rows,
            "content_files": hash_rows,
            "file_hash_audit": {
                "relative_path": hash_audit_path.name,
                "size_bytes": hash_audit_path.stat().st_size,
                "sha256": file_sha256(hash_audit_path),
            },
        }
        manifest_path = staging / "13_bundle_closure_manifest.json"
        _write_json(manifest_path, closure_manifest)
        _write_text(
            staging / "13_bundle_closure_manifest.sha256",
            f"{file_sha256(manifest_path)}  {manifest_path.name}",
        )
        audit_built_bundle(staging)

        return _publish_staged_bundle(
            staging,
            bundle_dir,
            output_root=output_root,
        )
    finally:
        if staging.exists():
            _safe_remove_staging(staging, output_root=output_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or audit the canonical PPH final review bundle.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE_DIR)
    build.add_argument("--acceptance-root", type=Path, default=DEFAULT_ACCEPTANCE_ROOT)
    build.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE_DIR)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(
                bundle_dir=args.bundle_dir,
                acceptance_root=args.acceptance_root,
                output_root=args.output_root,
            )
        else:
            result = audit_built_bundle(args.bundle_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
