"""Quarantined, source-bound corrections for reused Stage 1 summaries.

This module prepares a review candidate from a READY
``stage1_canonical_summaries`` artifact. It never edits that source artifact,
publishes a Stage 1 reuse manifest, advances a current pointer, or calls a
provider. A candidate remains unusable as Stage 1 input until an owner-facing
approval/promotion path is implemented outside this module.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast

from runtime.provider_runtime import hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.job_workspace import JobWorkspace, publish_bytes_artifact, publish_json_artifact
from services.paper_identity import build_canonical_paper_key, normalize_doi


PROPOSAL_ARTIFACT_TYPE = "stage1_summary_correction_proposal"
SOURCE_SNAPSHOT_ARTIFACT_TYPE = "stage1_summary_correction_source_snapshot"
CANDIDATE_ARTIFACT_TYPE = "stage1_summary_correction_candidate"
CORRECTION_ARTIFACT_VERSION = "v1"

_CONTENT_FIELD_PATHS = frozenset(
    {
        "ai_summary.core_analysis.summary",
        "ai_summary.core_analysis.methodology",
        "ai_summary.core_analysis.findings",
        "ai_summary.core_analysis.conclusions",
        "ai_summary.core_analysis.relevance",
        "ai_summary.core_analysis.limitations",
        "ai_summary.specialized_details.empirical.data_source_and_size",
        "ai_summary.specialized_details.empirical.analysis_technique",
        "ai_summary.specialized_details.empirical.sample_characteristics_or_context",
    }
)
_KEY_POINT_PATH = re.compile(r"^ai_summary\.core_analysis\.key_points\[(\d+)\]$")
_EXPERIMENT_MENTION = re.compile(r"\b(?:experiment|exp)\.?\s*([1-9][0-9]*)\b", re.IGNORECASE)
_ENTRY_LOCATOR = re.compile(r"^summaries\[(\d+)\]")
_PROPOSED_PREFIX = "PROPOSED_"


class SourceSummaryCorrectionError(ValueError):
    """Raised when a source-bound summary correction cannot be prepared safely."""


@dataclass(frozen=True)
class SourceSummaryFieldEditV1:
    field_path: str
    before_value: Any
    before_value_hash: str
    proposed_after_value: str
    proposed_after_value_hash: str
    declared_study_groups: tuple[str, ...]
    source_page_bindings: tuple[Mapping[str, Any], ...]
    binding_diagnostics: tuple[str, ...] = ()
    proposal_field_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_path": self.field_path,
            "before_value": copy.deepcopy(self.before_value),
            "before_value_hash": self.before_value_hash,
            "proposed_after_value": self.proposed_after_value,
            "proposed_after_value_hash": self.proposed_after_value_hash,
            "declared_study_groups": list(self.declared_study_groups),
            "source_page_bindings": [copy.deepcopy(dict(item)) for item in self.source_page_bindings],
            "binding_diagnostics": list(self.binding_diagnostics),
            "proposal_field_ref": self.proposal_field_ref,
        }


@dataclass(frozen=True)
class SourceSummaryCorrectionProposalV1:
    """Normalized v1 proposal, adapted from the reviewed Tripathi proposal shape."""

    proposal_id: str
    source_job_id: str
    source_summary_artifact_id: str
    source_summary_artifact_hash: str
    source_summary_set_hash: str
    target_canonical_paper_key: str
    target_entry_index: int
    target_doi: str
    target_citation_key: str
    before_summary_hash: str
    source_pdf_path: str
    source_pdf_sha256: str
    source_pdf_page_count: int
    field_edits: tuple[SourceSummaryFieldEditV1, ...]
    study_mappings: tuple[Mapping[str, Any], ...]
    source_conflicts: tuple[Mapping[str, Any], ...]
    source_authority_refs: Mapping[str, Any]
    source_entry_provenance: Mapping[str, Any]
    original_payload_hash: str
    original_payload: Mapping[str, Any]
    source_pointer_snapshot: Mapping[str, Any] | None = None
    adapter_diagnostics: tuple[str, ...] = ()

    schema_version: str = field(default="v1", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "source_job_id": self.source_job_id,
            "source_summary_artifact_id": self.source_summary_artifact_id,
            "source_summary_artifact_hash": self.source_summary_artifact_hash,
            "source_summary_set_hash": self.source_summary_set_hash,
            "target_canonical_paper_key": self.target_canonical_paper_key,
            "target_entry_index": self.target_entry_index,
            "target_doi": self.target_doi,
            "target_citation_key": self.target_citation_key,
            "before_summary_hash": self.before_summary_hash,
            "source_pdf_path": self.source_pdf_path,
            "source_pdf_sha256": self.source_pdf_sha256,
            "source_pdf_page_count": self.source_pdf_page_count,
            "field_edits": [item.to_dict() for item in self.field_edits],
            "study_mappings": [copy.deepcopy(dict(item)) for item in self.study_mappings],
            "source_conflicts": [copy.deepcopy(dict(item)) for item in self.source_conflicts],
            "source_authority_refs": copy.deepcopy(dict(self.source_authority_refs)),
            "source_entry_provenance": copy.deepcopy(dict(self.source_entry_provenance)),
            "original_payload_hash": self.original_payload_hash,
            "original_payload": copy.deepcopy(dict(self.original_payload)),
            "source_pointer_snapshot": copy.deepcopy(dict(self.source_pointer_snapshot))
            if self.source_pointer_snapshot is not None
            else None,
            "adapter_diagnostics": list(self.adapter_diagnostics),
        }


@dataclass(frozen=True)
class SourceSummaryCorrectionPreparationV1:
    status: str
    source_snapshot_artifact_id: str
    proposal_artifact_id: str
    candidate_artifact_id: str
    source_summary_set_hash: str
    candidate_summary_set_hash: str
    source_registry_unchanged: bool
    requires_owner_approval: bool = True
    usable_as_stage1_reuse: bool = False
    canonical_pointer_advanced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceSummaryCorrectionVerificationV1:
    verified: bool
    candidate_artifact_id: str
    candidate_status: str
    source_summary_artifact_id: str
    source_summary_set_hash: str
    candidate_summary_set_hash: str
    identity_set_preserved: bool
    source_closure_reverified: bool
    requires_owner_approval: bool = True
    usable_as_stage1_reuse: bool = False
    canonical_pointer_advanced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SourceSummaryCorrectionError(message)


def _require_mapping(value: object, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceSummaryCorrectionError(message)
    return cast(Mapping[str, Any], value)


def _require_list(value: object, message: str) -> list[object]:
    if not isinstance(value, list):
        raise SourceSummaryCorrectionError(message)
    return cast(list[object], value)


def _require_mapping_list(value: object, message: str) -> list[Mapping[str, Any]]:
    values = _require_list(value, message)
    return [
        _require_mapping(item, f"{message} entry {index} is not an object")
        for index, item in enumerate(values)
    ]


def _require_nonempty_list(value: object, message: str) -> list[object]:
    values = _require_list(value, message)
    _require(bool(values), message)
    return values


def _require_text(value: object, message: str) -> str:
    if not isinstance(value, str):
        raise SourceSummaryCorrectionError(message)
    return value


def _require_nonempty_text(value: object, message: str) -> str:
    text = _require_text(value, message)
    _require(bool(text.strip()), message)
    return text


def _require_positive_int(value: object, message: str) -> int:
    if type(value) is not int or value <= 0:
        raise SourceSummaryCorrectionError(message)
    return value


def _record_ref(record: ArtifactRecord) -> dict[str, Any]:
    return {
        "artifact_id": record.artifact_id,
        "artifact_role": record.artifact_role,
        "artifact_type": record.artifact_type,
        "artifact_version": record.artifact_version,
        "path": record.path,
        "producer": record.producer,
        "job_id": record.job_id,
        "status": record.status,
        "content_hash": record.content_hash,
        "depends_on": [item.to_dict() for item in record.depends_on],
        "metadata": copy.deepcopy(record.metadata),
    }


def _pointer_snapshot(registry: ArtifactRegistry) -> dict[str, Any] | None:
    record = registry.get("stage1_summaries")
    if record is None:
        return None
    return {
        "artifact_id": record.artifact_id,
        "artifact_type": record.artifact_type,
        "artifact_version": record.artifact_version,
        "status": record.status,
        "content_hash": record.content_hash,
        "current_version_artifact_id": str(record.metadata.get("current_version_artifact_id") or ""),
        "summary_set_hash": str(record.metadata.get("summary_set_hash") or ""),
    }


def _source_summary_document(record: ArtifactRecord) -> tuple[dict[str, Any], list[dict[str, Any]], bytes]:
    _require(record.status == "ready", "source Stage1 summary artifact must be READY")
    _require(
        record.artifact_type == "stage1_canonical_summaries" and record.artifact_version == "v1",
        "source artifact must be a registered stage1_canonical_summaries/v1 artifact",
    )
    _require(record.artifact_role == "stage1_input", "source Stage1 summary artifact role is invalid")
    path = Path(record.path)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceSummaryCorrectionError(f"cannot read source Stage1 summary artifact: {exc}") from exc
    _require(file_sha256(path) == record.content_hash, "source Stage1 summary artifact content hash is stale")
    payload = _require_mapping(payload, "Stage1 summary artifact must be an object envelope")
    _require(payload.get("artifact_type") == "stage1_canonical_summaries", "Stage1 summary envelope type mismatch")
    _require(payload.get("artifact_version") == "v1", "Stage1 summary envelope version mismatch")
    _require(str(payload.get("job_id") or "") == record.job_id, "Stage1 summary envelope job_id mismatch")
    summaries = _require_nonempty_list(
        payload.get("summaries"),
        "Stage1 summary envelope must contain a nonempty summary list",
    )
    copied_summaries: list[dict[str, Any]] = []
    for index, summary in enumerate(summaries):
        summary_mapping = _require_mapping(summary, f"Stage1 summary entry {index} is not an object")
        copied_summaries.append(copy.deepcopy(dict(summary_mapping)))
    summary_set_hash = str(payload.get("summary_set_hash") or "")
    _require(bool(summary_set_hash), "Stage1 summary envelope has no summary_set_hash")
    _require(hash_json(copied_summaries) == summary_set_hash, "Stage1 summary_set_hash does not match the summary list")
    recorded_hash = str(record.metadata.get("summary_set_hash") or "")
    _require(not recorded_hash or recorded_hash == summary_set_hash, "Registry summary_set_hash disagrees with the source envelope")
    prefix = "outline-v3:stage1-summaries:"
    if record.artifact_id.startswith(prefix):
        _require(record.artifact_id[len(prefix):] == summary_set_hash, "content-addressed Stage1 summary ID is stale")
    return dict(payload), copied_summaries, raw


def _canonical_keys(summaries: Sequence[Mapping[str, Any]]) -> list[str]:
    keys: list[str] = []
    for index, summary in enumerate(summaries):
        paper_info = _require_mapping(summary.get("paper_info"), f"summary entry {index} has no paper_info object")
        key = build_canonical_paper_key(paper_info)
        _require(bool(key) and not key.startswith("source:"), f"summary entry {index} has no canonical paper key")
        keys.append(key)
    return keys


def _parse_field_path(path: Any, summary: Mapping[str, Any]) -> tuple[str | int, ...]:
    field_path = _require_text(path, "field path must be a nonempty canonical string")
    _require(bool(field_path) and field_path == field_path.strip(), "field path must be a nonempty canonical string")
    if field_path in _CONTENT_FIELD_PATHS:
        return tuple(field_path.split("."))
    match = _KEY_POINT_PATH.fullmatch(field_path)
    if match:
        index = int(match.group(1))
        ai_summary = _require_mapping(summary.get("ai_summary"), "target summary has no ai_summary object")
        core_analysis = _require_mapping(ai_summary.get("core_analysis"), "target summary has no core_analysis object")
        key_points = _require_list(core_analysis.get("key_points"), "target summary key_points is not a list")
        _require(0 <= index < len(key_points), f"key_points index is outside the target summary: {field_path}")
        return ("ai_summary", "core_analysis", "key_points", index)
    raise SourceSummaryCorrectionError(f"unsupported Stage1 content field path: {field_path!r}")


def _get_path(value: Any, tokens: Sequence[str | int]) -> Any:
    current = value
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return _MISSING
            current = current[token]
        else:
            if not isinstance(current, Mapping) or token not in current:
                return _MISSING
            current = current[token]
    return current


def _set_path(value: Any, tokens: Sequence[str | int], replacement: Any) -> None:
    current = value
    for token in tokens[:-1]:
        current = current[token]
    current[tokens[-1]] = copy.deepcopy(replacement)


_MISSING = object()


def _read_pdf(path_value: Any, expected_sha256: Any, expected_page_count: Any) -> tuple[Path, str, int]:
    path_text = _require_nonempty_text(path_value, "source PDF path is required")
    path = Path(path_text).expanduser().resolve()
    _require(path.is_file(), "source PDF does not exist or is not a file")
    actual_hash = file_sha256(path)
    _require(
        isinstance(expected_sha256, str) and actual_hash == expected_sha256,
        "source PDF SHA-256 does not match the proposal",
    )
    try:
        import pymupdf

        document = pymupdf.open(path)
        try:
            page_count = len(document)
        finally:
            document.close()
    except Exception as exc:  # PyMuPDF reports several format-specific exception types.
        raise SourceSummaryCorrectionError(f"cannot validate source PDF pages: {exc}") from exc
    _require(page_count > 0, "source PDF contains no pages")
    if expected_page_count is not None:
        _require(
            type(expected_page_count) is int and expected_page_count == page_count,
            "source PDF page_count does not match the file",
        )
    return path, actual_hash, page_count


def _verify_page_binding(binding: Any, *, pdf_sha256: str, page_count: int) -> dict[str, Any]:
    binding = _require_mapping(binding, "PDF page binding must be an object")
    page = binding.get("pdf_page")
    _require(type(page) is int and 1 <= page <= page_count, "PDF page binding is out of range")
    _require(str(binding.get("source_pdf_sha256") or "") == pdf_sha256, "PDF page binding SHA-256 mismatch")
    journal_page = binding.get("journal_page")
    _require(type(journal_page) is int and journal_page > 0, "journal_page must be a positive integer")
    scope = str(binding.get("anchor_scope") or "").strip()
    _require(bool(scope), "PDF page binding requires a nonempty anchor_scope")
    rendered_path = str(binding.get("review_render_path") or "").strip()
    rendered_hash = str(binding.get("review_render_sha256") or "").strip()
    _require(bool(rendered_path) == bool(rendered_hash), "page render path/hash must be supplied together")
    if rendered_path:
        path = Path(rendered_path).expanduser().resolve()
        _require(path.is_file(), f"review page render is missing: {rendered_path}")
        _require(file_sha256(path) == rendered_hash, f"review page render hash mismatch: {rendered_path}")
    return copy.deepcopy(dict(binding))


def _study_mapping_index(payload: Mapping[str, Any], *, pdf_sha256: str, page_count: int) -> dict[int, dict[str, Any]]:
    rows = _require_nonempty_list(payload.get("proposed_study_mapping"), "proposal has no proposed study mapping")
    by_number: dict[int, dict[str, Any]] = {}
    seen_refs: set[str] = set()
    for row_index, raw_row in enumerate(rows):
        raw_row_mapping = _require_mapping(raw_row, f"proposed study mapping row {row_index} is not an object")
        row = copy.deepcopy(dict(raw_row_mapping))
        number = _require_positive_int(
            row.get("reported_experiment_number"),
            f"study mapping row {row_index} has invalid experiment number",
        )
        _require(number not in by_number, f"duplicate proposed experiment mapping: {number}")
        study_ref = str(row.get("proposed_study_ref") or "")
        _require(study_ref.startswith("PROPOSED_STUDY_REF:"), "study mapping IDs must remain visibly proposed")
        _require(study_ref not in seen_refs, f"duplicate proposed study ref: {study_ref}")
        seen_refs.add(study_ref)
        _require(row.get("canonical_study_id") is None, "proposal must not assign a canonical study ID")
        for id_list in ("source_claim_ids", "evidence_ids", "provider_receipt_ids"):
            _require(row.get(id_list) == [], f"proposal must not invent {id_list}")
        _require(str(row.get("status") or "") == "PROPOSED_MAPPING_ONLY", "study mapping must remain proposal-only")
        source_paths = _require_list(row.get("source_field_paths"), "study mapping source_field_paths must be a list")
        for path in source_paths:
            # Reuse the same allowlist for references; a field path never grants write authority.
            _parse_field_path(path, {"ai_summary": {"core_analysis": {"key_points": [""] * 16}}})
        bindings = _require_nonempty_list(row.get("page_bindings"), f"study mapping {number} has no PDF page bindings")
        row["page_bindings"] = [
            _verify_page_binding(item, pdf_sha256=pdf_sha256, page_count=page_count)
            for item in bindings
        ]
        by_number[number] = row
    return by_number


def _group_number(value: Any) -> int:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(?:exp|experiment)[\s._-]*([1-9][0-9]*)", text)
    if not match:
        raise SourceSummaryCorrectionError(f"unsupported study group reference: {value!r}")
    return int(match.group(1))


def _resolve_field_page_bindings(
    field_patch: Mapping[str, Any],
    *,
    field_path: str,
    study_by_number: Mapping[int, Mapping[str, Any]],
    pdf_sha256: str,
    page_count: int,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    raw_groups = _require_list(field_patch.get("source_anchor_groups"), f"{field_path} source_anchor_groups must be a list")
    declared_numbers = {_group_number(item) for item in raw_groups}
    text = str(field_patch.get("proposed_after_value") or "")
    mentioned_numbers = {int(match.group(1)) for match in _EXPERIMENT_MENTION.finditer(text)}
    unknown_numbers = sorted((declared_numbers | mentioned_numbers) - set(study_by_number))
    _require(not unknown_numbers, f"{field_path} references study mappings with no source pages: {unknown_numbers}")
    resolved_numbers = declared_numbers | mentioned_numbers
    if not resolved_numbers:
        resolved_numbers = {
            number
            for number, row in study_by_number.items()
            if field_path
            in _require_list(
                row.get("source_field_paths", []),
                f"study mapping {number} source_field_paths must be a list",
            )
        }
    _require(bool(resolved_numbers), f"{field_path} has no source study/page binding")
    bindings: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for number in sorted(resolved_numbers):
        row = study_by_number[number]
        page_bindings = _require_mapping_list(
            row.get("page_bindings"),
            f"study mapping {number} page_bindings must be a list of objects",
        )
        for item in page_bindings:
            key = (
                item["pdf_page"],
                item["journal_page"],
                item["source_pdf_sha256"],
                item["anchor_scope"],
            )
            if key not in seen:
                seen.add(key)
                bindings.append(copy.deepcopy(dict(item)))
    _require(bool(bindings), f"{field_path} resolved to no PDF page bindings")
    diagnostics: list[str] = []
    for number in sorted(mentioned_numbers - declared_numbers):
        diagnostics.append(
            f"Experiment {number} binding resolved from its explicit text reference "
            "to the proposal page map; original source_anchor_groups did not enumerate it"
        )
    return tuple(bindings), tuple(diagnostics)


def _same_pointer(registry: ArtifactRegistry, snapshot: Mapping[str, Any] | None) -> bool:
    return _pointer_snapshot(registry) == (dict(snapshot) if snapshot is not None else None)


def _artifact_dependency_ids(record: ArtifactRecord) -> list[dict[str, Any]]:
    return [copy.deepcopy(item.to_dict()) for item in record.depends_on]


def _validate_source_authority_references(
    payload: Mapping[str, Any],
    *,
    source_record: ArtifactRecord,
    source_doc: Mapping[str, Any],
    source_file_sha256: str,
) -> tuple[Mapping[str, Any], int]:
    _require(payload.get("artifact_type") == PROPOSAL_ARTIFACT_TYPE, "unsupported source correction proposal artifact_type")
    _require(payload.get("artifact_version") == "v1-proposal", "unsupported source correction proposal version")
    _require(str(payload.get("status") or "") == "PROPOSED_ONLY_NOT_CANONICAL_NOT_APPLIED", "proposal is not marked proposal-only")
    proposal_id = str(payload.get("proposal_id") or "")
    _require(proposal_id.startswith(_PROPOSED_PREFIX), "proposal_id must be visibly proposed")
    authority = _require_mapping(payload.get("source_authority"), "proposal has no source_authority object")
    _require(str(authority.get("artifact_id") or "") == source_record.artifact_id, "proposal source artifact ID mismatch")
    _require(str(authority.get("artifact_content_hash") or "") == source_record.content_hash, "proposal source artifact hash is stale")
    _require(str(authority.get("summary_set_hash") or "") == str(source_doc.get("summary_set_hash") or ""), "proposal summary_set_hash is stale")
    _require(str(authority.get("artifact_type") or "") == source_record.artifact_type, "proposal source artifact type mismatch")
    _require(str(authority.get("artifact_version") or "") == source_record.artifact_version, "proposal source artifact version mismatch")
    _require(str(authority.get("job_id") or "") == source_record.job_id, "proposal source job ID mismatch")
    _require(str(authority.get("summary_file_sha256") or "") == source_file_sha256, "proposal source summary file SHA-256 mismatch")
    source_summaries = _require_mapping_list(
        source_doc.get("summaries"),
        "source Stage1 summary list is unavailable",
    )
    _require(int(authority.get("summary_count") or -1) == len(source_summaries), "proposal summary count is stale")
    _require(str(authority.get("summary_file_path") or "") == source_record.path, "proposal source summary path mismatch")
    _require_mapping(authority.get("existing_entry_provenance"), "proposal has no existing entry provenance")
    locator = str(authority.get("target_entry_locator") or "")
    match = _ENTRY_LOCATOR.match(locator)
    if match is None:
        raise SourceSummaryCorrectionError("proposal target_entry_locator is malformed")
    index = int(match.group(1))
    _require(index < len(source_summaries), "proposal target entry locator is out of range")
    return authority, index


def _validate_proposal_flags(payload: Mapping[str, Any]) -> None:
    gates = _require_mapping(payload.get("schema_and_review_gates"), "proposal has no schema_and_review_gates object")
    for name in (
        "new_canonical_source_claim_ids",
        "new_canonical_evidence_ids",
        "new_provider_receipt_ids",
        "new_canonical_study_ids",
        "new_canonical_source_field_ids",
    ):
        _require(gates.get(name) == [], f"proposal must not invent {name}")
    mutations = _require_mapping(payload.get("mutations"), "proposal has no mutation boundary")
    _require(mutations.get("source_summary_modified") is False, "proposal already claims a source summary mutation")
    _require(mutations.get("source_pdf_modified") is False, "proposal already claims a source PDF mutation")
    _require(mutations.get("registry_written") is False, "proposal already claims a Registry write")
    _require(mutations.get("provider_calls") == 0, "proposal must not claim provider calls")
    _require(mutations.get("canonical_pointer_advanced") is False, "proposal must not advance a canonical pointer")
    _require(mutations.get("candidate_registered") is False, "proposal must not claim a registered candidate")
    _require(mutations.get("production_repair_applied") is False, "proposal must not claim an applied repair")


def _adapt_tripathi_proposal(
    payload: Mapping[str, Any],
    *,
    source_record: ArtifactRecord,
    source_doc: Mapping[str, Any],
    source_bytes_hash: str,
    source_pdf_page_count: int,
    source_pointer_snapshot: Mapping[str, Any] | None,
) -> SourceSummaryCorrectionProposalV1:
    authority, target_index = _validate_source_authority_references(
        payload,
        source_record=source_record,
        source_doc=source_doc,
        source_file_sha256=source_bytes_hash,
    )
    _validate_proposal_flags(payload)
    summaries = _require_mapping_list(source_doc.get("summaries"), "source Stage1 summary list is unavailable")
    target_entry = summaries[target_index]
    paper_info = _require_mapping(target_entry.get("paper_info"), "target summary entry has no paper_info object")
    proposed_identity = _require_mapping(authority.get("paper_identity"), "proposal paper_identity is missing")
    target_key = build_canonical_paper_key(paper_info)
    _require(bool(target_key) and not target_key.startswith("source:"), "target has no canonical paper key")
    proposed_key = build_canonical_paper_key(proposed_identity)
    _require(proposed_key == target_key, "proposal target resolves to a different canonical paper key")
    identity_keys = _canonical_keys(summaries)
    _require(identity_keys.count(target_key) == 1, "target canonical paper key is ambiguous in the Stage1 summary set")
    current_doi = normalize_doi(paper_info.get("doi"))
    proposed_doi = normalize_doi(proposed_identity.get("doi"))
    _require(bool(current_doi) and current_doi == proposed_doi, "proposal DOI does not uniquely match the target summary")
    current_citation_key = str(paper_info.get("citation_key") or paper_info.get("citation_key_raw") or "")
    proposed_citation_key = str(proposed_identity.get("citation_key") or "")
    _require(bool(current_citation_key) and current_citation_key == proposed_citation_key, "proposal citation_key does not match the target summary")
    if proposed_identity.get("title"):
        _require(str(proposed_identity.get("title")) == str(paper_info.get("title") or ""), "proposal title does not match the target summary")
    _require(
        dict(authority.get("existing_entry_provenance") or {}) == dict(target_entry.get("provenance") or {}),
        "proposal existing entry provenance does not match the source summary",
    )
    _require(
        authority.get("existing_entry_status") == target_entry.get("status")
        and authority.get("existing_source_mode") == target_entry.get("source_mode"),
        "proposal summary entry status/source_mode is stale",
    )

    proposal_pdf = _require_mapping(payload.get("source_pdf"), "proposal source_pdf binding is missing")
    pdf_path, pdf_hash, actual_page_count = _read_pdf(
        proposal_pdf.get("path"),
        proposal_pdf.get("sha256"),
        proposal_pdf.get("page_count"),
    )
    _require(actual_page_count == source_pdf_page_count, "PDF page count changed during proposal adaptation")
    summary_pdf_path_value = _require_nonempty_text(
        paper_info.get("pdf_path") or paper_info.get("source_pdf"),
        "target summary has no source PDF path",
    )
    summary_pdf_path = Path(summary_pdf_path_value).expanduser().resolve()
    _require(summary_pdf_path.is_file(), "target summary's original PDF path is missing")
    _require(file_sha256(summary_pdf_path) == pdf_hash, "proposal PDF bytes do not match the target summary source PDF")

    mappings = _study_mapping_index(payload, pdf_sha256=pdf_hash, page_count=actual_page_count)
    raw_patches = _require_nonempty_list(payload.get("source_field_patches"), "proposal has no source_field_patches")
    edits: list[SourceSummaryFieldEditV1] = []
    seen_paths: set[str] = set()
    for raw_patch in raw_patches:
        patch = dict(_require_mapping(raw_patch, "source_field_patches entries must be objects"))
        field_path = str(patch.get("canonical_stage1_field_path") or "")
        _require(field_path not in seen_paths, f"duplicate source field edit: {field_path}")
        seen_paths.add(field_path)
        tokens = _parse_field_path(field_path, target_entry)
        current_value = _get_path(target_entry, tokens)
        _require(current_value is not _MISSING, f"source field does not exist: {field_path}")
        _require(current_value == patch.get("before_value"), f"stale before value for source field: {field_path}")
        before_hash = hash_json(current_value)
        _require(before_hash == str(patch.get("before_value_hash_json") or ""), f"stale before hash for source field: {field_path}")
        after_value = _require_nonempty_text(
            patch.get("proposed_after_value"),
            f"proposed content field must be nonempty text: {field_path}",
        )
        after_hash = hash_json(after_value)
        _require(after_hash == str(patch.get("proposed_after_value_hash_json") or ""), f"proposed after hash mismatch for source field: {field_path}")
        field_ref = str(patch.get("proposal_field_ref") or "")
        _require(field_ref.startswith("PROPOSED_SOURCE_FIELD_REF:"), f"field ref must be visibly proposed: {field_path}")
        downstream_ref = patch.get("existing_downstream_outline_field_ref")
        if downstream_ref is not None:
            _require(isinstance(downstream_ref, Mapping), f"downstream field reference is malformed: {field_path}")
            _require(downstream_ref.get("canonical_stage1_field_id") is False, f"downstream projection ID cannot be adopted as a canonical Stage1 field ID: {field_path}")
        bindings, binding_diagnostics = _resolve_field_page_bindings(
            patch,
            field_path=field_path,
            study_by_number=mappings,
            pdf_sha256=pdf_hash,
            page_count=actual_page_count,
        )
        edits.append(
            SourceSummaryFieldEditV1(
                field_path=field_path,
                before_value=copy.deepcopy(current_value),
                before_value_hash=before_hash,
                proposed_after_value=after_value,
                proposed_after_value_hash=after_hash,
                declared_study_groups=tuple(
                    str(item)
                    for item in _require_list(
                        patch.get("source_anchor_groups"),
                        f"{field_path} source_anchor_groups must be a list",
                    )
                ),
                source_page_bindings=bindings,
                binding_diagnostics=binding_diagnostics,
                proposal_field_ref=field_ref,
            )
        )

    raw_conflicts = _require_list(payload.get("source_conflicts"), "proposal source_conflicts must be a list")
    conflicts: list[Mapping[str, Any]] = []
    for conflict in raw_conflicts:
        conflict = _require_mapping(conflict, "source conflict entries must be objects")
        conflict_ref = str(conflict.get("proposal_conflict_ref") or "")
        _require(conflict_ref.startswith("PROPOSED_CONFLICT_REF:"), "source conflict ref must remain proposal-only")
        conflict_path = str(conflict.get("summary_field_path") or "")
        _parse_field_path(conflict_path, target_entry)
        _require(str(conflict.get("resolution") or "").startswith("UNRESOLVED_"), "source conflict must remain explicitly unresolved")
        conflict_anchor = conflict.get("pdf_anchor")
        _verify_page_binding(conflict_anchor, pdf_sha256=pdf_hash, page_count=actual_page_count)
        conflicts.append(copy.deepcopy(dict(conflict)))

    entry_locator = str(authority.get("target_entry_locator") or "")
    before_summary_hash = hash_json(target_entry)
    mappings_normalized = tuple(copy.deepcopy(dict(item)) for item in mappings.values())
    return SourceSummaryCorrectionProposalV1(
        proposal_id=str(payload["proposal_id"]),
        source_job_id=source_record.job_id,
        source_summary_artifact_id=source_record.artifact_id,
        source_summary_artifact_hash=source_record.content_hash,
        source_summary_set_hash=str(source_doc["summary_set_hash"]),
        target_canonical_paper_key=target_key,
        target_entry_index=target_index,
        target_doi=current_doi,
        target_citation_key=current_citation_key,
        before_summary_hash=before_summary_hash,
        source_pdf_path=str(pdf_path),
        source_pdf_sha256=pdf_hash,
        source_pdf_page_count=actual_page_count,
        field_edits=tuple(edits),
        study_mappings=mappings_normalized,
        source_conflicts=tuple(conflicts),
        source_authority_refs={
            "artifact": _record_ref(source_record),
            "entry_locator": entry_locator,
            "entry_provenance": copy.deepcopy(dict(target_entry.get("provenance") or {})),
            "source_pdf_path_from_summary": str(summary_pdf_path),
            "source_pdf_sha256": pdf_hash,
            "source_pointer_snapshot": copy.deepcopy(dict(source_pointer_snapshot))
            if source_pointer_snapshot is not None
            else None,
        },
        source_entry_provenance=copy.deepcopy(dict(target_entry.get("provenance") or {})),
        original_payload_hash=hash_json(payload),
        original_payload=copy.deepcopy(dict(payload)),
        source_pointer_snapshot=copy.deepcopy(dict(source_pointer_snapshot))
        if source_pointer_snapshot is not None
        else None,
        adapter_diagnostics=tuple(
            diagnostic
            for edit in edits
            for diagnostic in edit.binding_diagnostics
        ),
    )


def _apply_field_edits(
    summaries: Sequence[Mapping[str, Any]],
    proposal: SourceSummaryCorrectionProposalV1,
) -> list[dict[str, Any]]:
    candidate_summaries = [copy.deepcopy(dict(item)) for item in summaries]
    target = candidate_summaries[proposal.target_entry_index]
    protected_before = {
        key: copy.deepcopy(target.get(key))
        for key in ("status", "source_mode", "paper_info", "provenance")
    }
    for edit in proposal.field_edits:
        tokens = _parse_field_path(edit.field_path, target)
        current = _get_path(target, tokens)
        _require(current == edit.before_value, f"source field changed after proposal normalization: {edit.field_path}")
        _require(hash_json(current) == edit.before_value_hash, f"source field hash changed after proposal normalization: {edit.field_path}")
        _require(hash_json(edit.proposed_after_value) == edit.proposed_after_value_hash, f"proposed field hash changed: {edit.field_path}")
        _set_path(target, tokens, edit.proposed_after_value)
    for key, original in protected_before.items():
        _require(target.get(key) == original, f"protected source identity/authority field was changed: {key}")
    _require(len(candidate_summaries) == len(summaries), "candidate changed Stage1 corpus cardinality")
    _require(_canonical_keys(candidate_summaries) == _canonical_keys(summaries), "candidate changed the Stage1 paper identity set/order")
    return candidate_summaries


def _validate_candidate_summary_payload(
    payload: Mapping[str, Any],
    *,
    source_doc: Mapping[str, Any],
    source_record: ArtifactRecord,
    proposal: SourceSummaryCorrectionProposalV1,
) -> list[dict[str, Any]]:
    candidate_summaries = _require_list(
        payload.get("candidate_summaries"),
        "quarantined candidate has no candidate_summaries list",
    )
    normalized_candidate = [
        dict(item)
        for item in _require_mapping_list(
            candidate_summaries,
            "candidate_summaries contains a non-object item",
        )
    ]
    source_summaries = _require_mapping_list(source_doc.get("summaries"), "source Stage1 summary list is unavailable")
    expected = _apply_field_edits(source_summaries, proposal)
    _require(normalized_candidate == expected, "quarantined candidate bytes do not match the normalized proposal edits")
    _require(len(normalized_candidate) == len(source_summaries), "candidate changed Stage1 corpus cardinality")
    _require(
        str(payload.get("source_summary_artifact_id") or "") == source_record.artifact_id
        and str(payload.get("source_summary_artifact_hash") or "") == source_record.content_hash,
        "candidate old authority refs do not match the original source artifact",
    )
    _require(str(payload.get("source_summary_set_hash") or "") == proposal.source_summary_set_hash, "candidate source summary_set_hash is stale")
    _require(str(payload.get("candidate_summary_set_hash") or "") == hash_json(normalized_candidate), "candidate summary set hash is invalid")
    _require(str(payload.get("target_before_summary_hash") or "") == proposal.before_summary_hash, "candidate before-summary hash is stale")
    after_hash = hash_json(normalized_candidate[proposal.target_entry_index])
    _require(str(payload.get("target_after_summary_hash") or "") == after_hash, "candidate after-summary hash is invalid")
    _require(payload.get("identity_set_preserved") is True, "candidate does not assert preserved identities")
    identity_hash = hash_json(_canonical_keys(source_summaries))
    _require(str(payload.get("source_identity_list_hash") or "") == identity_hash, "candidate source identity-list hash is invalid")
    _require(str(payload.get("candidate_identity_list_hash") or "") == hash_json(_canonical_keys(normalized_candidate)), "candidate identity-list hash is invalid")
    _require(payload.get("requires_owner_approval_before_adoption") is True, "candidate must require owner approval")
    _require(payload.get("usable_as_stage1_reuse") is False, "quarantined candidate must not be usable as Stage1 reuse")
    _require(payload.get("canonical_pointer_advanced") is False, "candidate must not advance a canonical pointer")
    _require(payload.get("provider_calls") == 0, "candidate must not claim provider calls")
    _require(payload.get("provider_receipt_ids_for_candidate") == [], "candidate must not create or reuse provider receipts")
    _require(payload.get("typed_reuse_manifest_created") is False, "candidate must not mint typed Stage1 reuse authority")
    return normalized_candidate


def _current_stage1_summary_record(
    source_registry: ArtifactRegistry,
    source_artifact_id: str,
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> ArtifactRecord:
    source_registry.reload()
    record = source_registry.get(source_artifact_id)
    _require(record is not None, f"registered Stage1 summary artifact is missing: {source_artifact_id}")
    assert record is not None
    _require(record.status == "ready", "source Stage1 summary artifact must be READY")
    _require(
        record.artifact_type == "stage1_canonical_summaries" and record.artifact_version == "v1",
        "source artifact must be a registered stage1_canonical_summaries/v1 artifact",
    )
    _require(record.artifact_role == "stage1_input", "source Stage1 summary artifact role is invalid")
    try:
        verified = source_registry.verify_ready_artifact_closure(
            record,
            external_registry_resolver=external_registry_resolver,
        )
    except (RegistryError, OSError, ValueError, TypeError) as exc:
        raise SourceSummaryCorrectionError(f"source Stage1 READY closure failed: {exc}") from exc
    _require(
        verified.artifact_id == record.artifact_id
        and verified.content_hash == record.content_hash
        and verified.job_id == record.job_id,
        "verified Stage1 source closure returned a different artifact identity",
    )
    return verified


def _validate_pointer_source(
    source_registry: ArtifactRegistry,
    source_record: ArtifactRecord,
) -> dict[str, Any] | None:
    pointer = _pointer_snapshot(source_registry)
    if pointer is None:
        return None
    _require(pointer.get("status") == "ready", "current stage1_summaries pointer is not READY")
    pointer_id = str(pointer.get("artifact_id") or "")
    versioned_id = str(pointer.get("current_version_artifact_id") or "")
    if pointer_id == "stage1_summaries":
        _require(bool(versioned_id), "stage1_summaries pointer has no current version artifact ID")
        _require(
            versioned_id == source_record.artifact_id,
            "proposal source artifact is not the current stage1_summaries version",
        )
    else:
        _require(
            pointer_id == source_record.artifact_id,
            "a different stage1_summaries artifact is current; proposal source is stale",
        )
    return pointer


def _destination_context_checks(
    *,
    source_registry: ArtifactRegistry,
    destination_workspace: JobWorkspace,
    destination_registry: ArtifactRegistry,
) -> None:
    _require(
        source_registry.job_id != destination_registry.job_id,
        "source and destination Registries must be separate jobs",
    )
    source_path = os.path.normcase(os.path.abspath(os.fspath(source_registry.registry_path)))
    destination_path = os.path.normcase(os.path.abspath(os.fspath(destination_registry.registry_path)))
    _require(source_path != destination_path, "source and destination Registry paths must be different")
    _require(
        destination_workspace.job_id == destination_registry.job_id,
        "destination JobWorkspace and Registry job_id differ",
    )
    workspace_registry_path = os.path.normcase(os.path.abspath(destination_workspace.paths.registry_path))
    _require(
        workspace_registry_path == destination_path,
        "destination Registry must belong to the destination JobWorkspace",
    )


def _write_quarantined_json(
    *,
    publication_context: Any,
    registry: ArtifactRegistry,
    workspace: JobWorkspace,
    payload: Mapping[str, Any],
    artifact_id: str,
    artifact_type: str,
    filename: str,
    depends_on: Sequence[ArtifactDependencyRefV2],
    metadata: Mapping[str, Any],
) -> ArtifactRecord:
    path = workspace.artifact_path(f"stage1_summary_correction/{filename}")
    record = publish_json_artifact(
        publication_context,
        registry,
        path,
        dict(payload),
        artifact_id=artifact_id,
        artifact_role="stage1_summary_correction_candidate",
        artifact_type=artifact_type,
        artifact_version=CORRECTION_ARTIFACT_VERSION,
        producer="services.summary_correction.prepare_source_summary_correction_candidate",
        status="quarantined",
        depends_on=list(depends_on),
        metadata={
            **dict(metadata),
            "canonical_replacement": False,
            "usable_as_stage1_reuse": False,
            "requires_owner_approval": True,
        },
    )
    _require(record.status == "quarantined", "publication did not quarantine the correction artifact")
    _require(record.job_id == registry.job_id, "published correction artifact belongs to a different Registry")
    _require(record.artifact_id == artifact_id and record.artifact_type == artifact_type, "published correction artifact identity mismatch")
    _require(file_sha256(record.path) == record.content_hash, "published correction artifact file hash mismatch")
    return record


def _write_quarantined_source_snapshot(
    *,
    publication_context: Any,
    registry: ArtifactRegistry,
    workspace: JobWorkspace,
    source_record: ArtifactRecord,
    source_registry_file_hash: str,
) -> ArtifactRecord:
    source_bytes = Path(source_record.path).read_bytes()
    _require(file_sha256(source_record.path) == source_record.content_hash, "source summary changed before snapshot publication")
    snapshot_key = hash_json(
        {
            "destination_job_id": registry.job_id,
            "source_job_id": source_record.job_id,
            "source_artifact_id": source_record.artifact_id,
            "source_artifact_hash": source_record.content_hash,
        }
    )[:24]
    snapshot_id = f"PROPOSED_STAGE1_SOURCE_SNAPSHOT__{snapshot_key}"
    snapshot_path = workspace.artifact_path(f"stage1_summary_correction/source_snapshots/{snapshot_key}.json")
    external_source_ref = ArtifactDependencyRefV2.from_record(
        source_record,
        dependency_kind="external_job",
    )
    record = publish_bytes_artifact(
        publication_context,
        registry,
        snapshot_path,
        source_bytes,
        artifact_id=snapshot_id,
        artifact_role="stage1_summary_correction_source_snapshot",
        artifact_type=SOURCE_SNAPSHOT_ARTIFACT_TYPE,
        artifact_version=CORRECTION_ARTIFACT_VERSION,
        producer="services.summary_correction.prepare_source_summary_correction_candidate",
        status="quarantined",
        depends_on=[external_source_ref],
        metadata={
            "source_authority_job_id": source_record.job_id,
            "source_authority_artifact_id": source_record.artifact_id,
            "source_authority_artifact_type": source_record.artifact_type,
            "source_authority_artifact_version": source_record.artifact_version,
            "source_authority_artifact_hash": source_record.content_hash,
            "source_registry_file_sha256_at_prepare": source_registry_file_hash,
            "source_record_dependencies": [item.to_dict() for item in source_record.depends_on],
            "source_summary_bytes_copied_exactly": True,
            "canonical_replacement": False,
            "usable_as_stage1_reuse": False,
            "requires_owner_approval": True,
        },
    )
    _require(record.status == "quarantined", "source snapshot must remain quarantined")
    _require(record.content_hash == source_record.content_hash, "source snapshot bytes differ from the original Stage1 artifact")
    _require(record.job_id == registry.job_id, "source snapshot was not published in the destination Registry")
    _require(record.artifact_id.startswith("PROPOSED_STAGE1_SOURCE_SNAPSHOT__"), "source snapshot ID must be visibly proposed")
    return record


def adapt_tripathi_source_summary_proposal_v1(
    proposal_payload: Mapping[str, Any],
    *,
    source_record: ArtifactRecord,
    source_document: Mapping[str, Any],
    source_file_sha256: str,
    source_pdf_page_count: int,
    source_pointer_snapshot: Mapping[str, Any] | None,
) -> SourceSummaryCorrectionProposalV1:
    """Adapt the reviewed Tripathi proposal JSON without treating its IDs as canonical.

    The adapter resolves its PROPOSED experiment references to the proposal's
    existing, source-PDF-bound page map. Text that mentions an experiment adds
    that experiment's existing page bindings to the field edit and records the
    resolution in ``binding_diagnostics``; if the map has no pages for that
    experiment, preparation fails closed.
    """

    payload = copy.deepcopy(dict(proposal_payload))
    return _adapt_tripathi_proposal(
        payload,
        source_record=source_record,
        source_doc=source_document,
        source_bytes_hash=source_file_sha256,
        source_pdf_page_count=source_pdf_page_count,
        source_pointer_snapshot=source_pointer_snapshot,
    )


def prepare_source_summary_correction_candidate(
    *,
    proposal_payload: Mapping[str, Any] | SourceSummaryCorrectionProposalV1,
    source_registry: ArtifactRegistry,
    source_artifact_id: str,
    destination_workspace: JobWorkspace,
    destination_registry: ArtifactRegistry,
    publication_context: Any,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> SourceSummaryCorrectionPreparationV1:
    """Prepare a quarantined candidate in a separate destination workspace.

    The source Registry is opened read-only: its READY closure and bytes are
    verified, copied to a quarantined destination snapshot, and referenced by
    quarantined proposal/candidate artifacts. No source Registry record,
    Stage1 current pointer, provider receipt, or typed reuse authority is
    created or changed.
    """

    _destination_context_checks(
        source_registry=source_registry,
        destination_workspace=destination_workspace,
        destination_registry=destination_registry,
    )
    destination_registry.reload()
    source_registry.reload()
    source_registry_path = Path(source_registry.registry_path)
    source_registry_file_hash_before = file_sha256(source_registry_path)
    source_registry_revision_before = source_registry.revision
    source_record = _current_stage1_summary_record(
        source_registry,
        source_artifact_id,
        external_registry_resolver=external_registry_resolver,
    )
    source_pointer = _validate_pointer_source(source_registry, source_record)
    source_doc, summaries, source_bytes = _source_summary_document(source_record)
    source_bytes_hash = file_sha256(Path(source_record.path))
    _require(source_bytes_hash == source_record.content_hash, "source summary artifact changed during preparation")

    if isinstance(proposal_payload, SourceSummaryCorrectionProposalV1):
        raw_proposal = _require_mapping(
            proposal_payload.original_payload,
            "typed proposal has no original Tripathi proposal payload",
        )
    else:
        raw_proposal = proposal_payload
    proposal_source_pdf = _require_mapping(raw_proposal.get("source_pdf"), "proposal source_pdf binding is missing")
    pdf_path, pdf_sha256, page_count = _read_pdf(
        proposal_source_pdf.get("path"),
        proposal_source_pdf.get("sha256"),
        proposal_source_pdf.get("page_count"),
    )
    proposal = adapt_tripathi_source_summary_proposal_v1(
        raw_proposal,
        source_record=source_record,
        source_document=source_doc,
        source_file_sha256=source_bytes_hash,
        source_pdf_page_count=page_count,
        source_pointer_snapshot=source_pointer,
    )
    if isinstance(proposal_payload, SourceSummaryCorrectionProposalV1):
        _require(
            hash_json(proposal.to_dict()) == hash_json(proposal_payload.to_dict()),
            "typed proposal differs from its source-bound original payload",
        )

    _require(proposal.source_pdf_sha256 == pdf_sha256, "proposal PDF hash changed during preparation")
    source_pdf_path_in_entry = str(
        summaries[proposal.target_entry_index].get("paper_info", {}).get("pdf_path")
        or summaries[proposal.target_entry_index].get("paper_info", {}).get("source_pdf")
        or ""
    )
    _require(bool(source_pdf_path_in_entry), "target Stage1 summary has no PDF path")
    source_pdf_path_in_entry = str(Path(source_pdf_path_in_entry).expanduser().resolve())
    _require(Path(source_pdf_path_in_entry).is_file(), "target Stage1 summary's original PDF path is missing")
    _require(file_sha256(source_pdf_path_in_entry) == pdf_sha256, "proposal PDF bytes do not match the Stage1 summary source PDF")

    source_identity_list = _canonical_keys(summaries)
    candidate_summaries = _apply_field_edits(summaries, proposal)
    candidate_identity_list = _canonical_keys(candidate_summaries)
    _require(source_identity_list == candidate_identity_list, "proposed correction changed the 63-paper identity set/order")
    _require(hash_json(candidate_summaries) != proposal.source_summary_set_hash, "proposal contains no effective Stage1 content changes")

    destination_pointer_before = _pointer_snapshot(destination_registry)
    source_registry_file_hash_before_snapshot = file_sha256(source_registry_path)
    _require(
        source_registry_file_hash_before_snapshot == source_registry_file_hash_before,
        "source Registry changed while the proposal was being validated",
    )
    source_snapshot_record = _write_quarantined_source_snapshot(
        publication_context=publication_context,
        registry=destination_registry,
        workspace=destination_workspace,
        source_record=source_record,
        source_registry_file_hash=source_registry_file_hash_before,
    )

    proposal_payload_normalized = {
        "artifact_type": PROPOSAL_ARTIFACT_TYPE,
        "artifact_version": CORRECTION_ARTIFACT_VERSION,
        "status": "quarantined",
        "proposal_id": proposal.proposal_id,
        "proposal_schema_version": proposal.schema_version,
        "normalized_proposal": proposal.to_dict(),
        "normalized_proposal_hash": hash_json(proposal.to_dict()),
        "source_snapshot_artifact_id": source_snapshot_record.artifact_id,
        "source_authority": {
            "job_id": source_record.job_id,
            "artifact_id": source_record.artifact_id,
            "artifact_type": source_record.artifact_type,
            "artifact_version": source_record.artifact_version,
            "artifact_hash": source_record.content_hash,
            "summary_set_hash": proposal.source_summary_set_hash,
            "before_summary_hash": proposal.before_summary_hash,
            "canonical_paper_key": proposal.target_canonical_paper_key,
            "doi": proposal.target_doi,
            "citation_key": proposal.target_citation_key,
            "source_pdf_sha256": proposal.source_pdf_sha256,
            "source_pdf_path": proposal.source_pdf_path,
        },
        "field_edits": [item.to_dict() for item in proposal.field_edits],
        "study_mappings": [copy.deepcopy(dict(item)) for item in proposal.study_mappings],
        "source_conflicts": [copy.deepcopy(dict(item)) for item in proposal.source_conflicts],
        "source_authority_refs_before_only": copy.deepcopy(dict(proposal.source_authority_refs)),
        "source_entry_provenance_before_only": copy.deepcopy(dict(proposal.source_entry_provenance)),
        "original_proposal_payload_hash": proposal.original_payload_hash,
        "normalized_proposal_hash": hash_json(proposal.to_dict()),
        "original_proposal_payload": copy.deepcopy(dict(proposal.original_payload)),
        "adapter_diagnostics": list(proposal.adapter_diagnostics),
        "requires_owner_approval_before_adoption": True,
        "usable_as_stage1_reuse": False,
        "provider_calls": 0,
        "provider_receipt_ids_for_candidate": [],
        "typed_reuse_manifest_created": False,
        "canonical_pointer_advanced": False,
    }
    proposal_fingerprint = hash_json(proposal_payload_normalized)
    proposal_artifact_id = f"PROPOSED_STAGE1_SUMMARY_CORRECTION_PROPOSAL__{proposal_fingerprint[:24]}"
    proposal_record = _write_quarantined_json(
        publication_context=publication_context,
        registry=destination_registry,
        workspace=destination_workspace,
        payload=proposal_payload_normalized,
        artifact_id=proposal_artifact_id,
        artifact_type=PROPOSAL_ARTIFACT_TYPE,
        filename=f"proposals/{proposal_fingerprint[:24]}.json",
        depends_on=[ArtifactDependencyRefV2.from_record(source_snapshot_record)],
        metadata={
            "proposal_id": proposal.proposal_id,
            "source_snapshot_artifact_id": source_snapshot_record.artifact_id,
        },
    )

    candidate_payload: dict[str, Any] = {
        "artifact_type": CANDIDATE_ARTIFACT_TYPE,
        "artifact_version": CORRECTION_ARTIFACT_VERSION,
        "status": "quarantined_prepared_bytes",
        "proposal_id": proposal.proposal_id,
        "proposal_artifact_id": proposal_record.artifact_id,
        "source_snapshot_artifact_id": source_snapshot_record.artifact_id,
        "source_authority": {
            "source_job_id": source_record.job_id,
            "source_artifact_id": source_record.artifact_id,
            "source_artifact_type": source_record.artifact_type,
            "source_artifact_version": source_record.artifact_version,
            "source_artifact_hash": source_record.content_hash,
            "source_registry_revision_at_prepare": source_registry_revision_before,
            "source_registry_file_sha256_at_prepare": source_registry_file_hash_before,
            "source_record_dependencies": _artifact_dependency_ids(source_record),
            "source_pointer_snapshot": copy.deepcopy(dict(source_pointer)) if source_pointer is not None else None,
            "source_summary_set_hash": proposal.source_summary_set_hash,
            "target_canonical_paper_key": proposal.target_canonical_paper_key,
            "target_entry_index": proposal.target_entry_index,
            "before_summary_hash": proposal.before_summary_hash,
            "source_pdf_path": proposal.source_pdf_path,
            "source_pdf_sha256": proposal.source_pdf_sha256,
            "source_pdf_page_count": proposal.source_pdf_page_count,
        },
        "source_summary_artifact_id": source_record.artifact_id,
        "source_summary_artifact_hash": source_record.content_hash,
        "source_summary_set_hash": proposal.source_summary_set_hash,
        "source_summary_file_sha256": source_bytes_hash,
        "target_before_summary_hash": proposal.before_summary_hash,
        "candidate_summaries": candidate_summaries,
        "candidate_summary_set_hash": hash_json(candidate_summaries),
        "target_after_summary_hash": hash_json(candidate_summaries[proposal.target_entry_index]),
        "field_changes": [item.to_dict() for item in proposal.field_edits],
        "normalized_proposal_hash": hash_json(proposal.to_dict()),
        "original_proposal_payload_hash": proposal.original_payload_hash,
        "source_conflicts_preserved": [copy.deepcopy(dict(item)) for item in proposal.source_conflicts],
        "source_entry_provenance_before_only": copy.deepcopy(dict(proposal.source_entry_provenance)),
        "source_identity_list_hash": hash_json(source_identity_list),
        "candidate_identity_list_hash": hash_json(candidate_identity_list),
        "summary_count_before": len(summaries),
        "summary_count_after": len(candidate_summaries),
        "identity_set_preserved": source_identity_list == candidate_identity_list,
        "requires_owner_approval_before_adoption": True,
        "usable_as_stage1_reuse": False,
        "canonical_replacement": False,
        "canonical_pointer_advanced": False,
        "provider_calls": 0,
        "provider_receipt_ids_for_candidate": [],
        "typed_reuse_manifest_created": False,
        "source_registry_mutation_performed_by_service": False,
        "source_registry_file_sha256_at_prepare_start": source_registry_file_hash_before,
        "destination_stage1_pointer_snapshot_before": copy.deepcopy(dict(destination_pointer_before))
        if destination_pointer_before is not None
        else None,
    }
    candidate_fingerprint = hash_json(candidate_payload)
    candidate_artifact_id = f"PROPOSED_STAGE1_SUMMARY_CORRECTION_CANDIDATE__{candidate_fingerprint[:24]}"
    candidate_record = _write_quarantined_json(
        publication_context=publication_context,
        registry=destination_registry,
        workspace=destination_workspace,
        payload=candidate_payload,
        artifact_id=candidate_artifact_id,
        artifact_type=CANDIDATE_ARTIFACT_TYPE,
        filename=f"candidates/{candidate_fingerprint[:24]}.json",
        depends_on=[
            ArtifactDependencyRefV2.from_record(source_snapshot_record),
            ArtifactDependencyRefV2.from_record(proposal_record),
        ],
        metadata={
            "proposal_id": proposal.proposal_id,
            "proposal_artifact_id": proposal_record.artifact_id,
            "source_snapshot_artifact_id": source_snapshot_record.artifact_id,
            "candidate_summary_set_hash": candidate_payload["candidate_summary_set_hash"],
        },
    )

    source_registry_file_hash_after = file_sha256(source_registry_path)
    source_registry_revision_after = source_registry.revision
    source_summary_file_hash_after = file_sha256(source_record.path)
    _require(
        source_registry_file_hash_after == source_registry_file_hash_before
        and source_registry_revision_after == source_registry_revision_before,
        "source Registry changed during correction-candidate preparation",
    )
    _require(source_summary_file_hash_after == source_record.content_hash, "source Stage1 summary changed during preparation")
    _require(_same_pointer(source_registry, source_pointer), "source stage1_summaries pointer changed during preparation")
    _require(_same_pointer(destination_registry, destination_pointer_before), "destination stage1_summaries pointer was changed")

    verify_source_summary_correction_candidate(
        source_registry=source_registry,
        destination_registry=destination_registry,
        candidate_artifact_id=candidate_record.artifact_id,
        external_registry_resolver=external_registry_resolver,
    )
    return SourceSummaryCorrectionPreparationV1(
        status="ready_for_owner_review",
        source_snapshot_artifact_id=source_snapshot_record.artifact_id,
        proposal_artifact_id=proposal_record.artifact_id,
        candidate_artifact_id=candidate_record.artifact_id,
        source_summary_set_hash=proposal.source_summary_set_hash,
        candidate_summary_set_hash=str(candidate_payload["candidate_summary_set_hash"]),
        source_registry_unchanged=True,
        requires_owner_approval=True,
        usable_as_stage1_reuse=False,
        canonical_pointer_advanced=False,
    )


def verify_source_summary_correction_candidate(
    *,
    source_registry: ArtifactRegistry,
    destination_registry: ArtifactRegistry,
    candidate_artifact_id: str,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> SourceSummaryCorrectionVerificationV1:
    """Reverify a quarantined candidate and its original READY source closure."""

    source_registry.reload()
    destination_registry.reload()
    candidate_record = destination_registry.get(candidate_artifact_id)
    _require(candidate_record is not None, f"quarantined correction candidate is missing: {candidate_artifact_id}")
    assert candidate_record is not None
    _require(candidate_record.status == "quarantined", "correction candidate is not quarantined")
    _require(candidate_record.artifact_type == CANDIDATE_ARTIFACT_TYPE, "candidate artifact type is invalid")
    _require(candidate_record.artifact_version == CORRECTION_ARTIFACT_VERSION, "candidate artifact version is invalid")
    _require(candidate_record.artifact_id.startswith("PROPOSED_STAGE1_SUMMARY_CORRECTION_CANDIDATE__"), "candidate artifact ID is not proposal-scoped")
    _require(file_sha256(candidate_record.path) == candidate_record.content_hash, "candidate artifact bytes were tampered")
    try:
        candidate_payload = json.loads(Path(candidate_record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceSummaryCorrectionError(f"candidate artifact is unreadable: {exc}") from exc
    _require(isinstance(candidate_payload, Mapping), "candidate artifact payload must be an object")
    _require(candidate_payload.get("artifact_type") == CANDIDATE_ARTIFACT_TYPE, "candidate payload type mismatch")
    _require(candidate_payload.get("artifact_version") == CORRECTION_ARTIFACT_VERSION, "candidate payload version mismatch")
    _require(candidate_payload.get("status") == "quarantined_prepared_bytes", "candidate payload status is invalid")
    _require(candidate_payload.get("requires_owner_approval_before_adoption") is True, "candidate has no owner-approval gate")
    _require(candidate_payload.get("usable_as_stage1_reuse") is False, "candidate cannot be Stage1 reuse authority")
    _require(candidate_payload.get("canonical_pointer_advanced") is False, "candidate payload claims a pointer change")
    _require(candidate_payload.get("provider_calls") == 0, "candidate payload claims provider calls")
    _require(candidate_payload.get("provider_receipt_ids_for_candidate") == [], "candidate payload contains provider receipts")
    _require(candidate_payload.get("typed_reuse_manifest_created") is False, "candidate payload claims a typed reuse manifest")

    source_snapshot_id = str(candidate_payload.get("source_snapshot_artifact_id") or "")
    proposal_id = str(candidate_payload.get("proposal_artifact_id") or "")
    source_snapshot_record = destination_registry.get(source_snapshot_id)
    proposal_record = destination_registry.get(proposal_id)
    _require(source_snapshot_record is not None, "candidate source snapshot is not registered in destination Registry")
    _require(proposal_record is not None, "candidate proposal artifact is not registered in destination Registry")
    assert source_snapshot_record is not None and proposal_record is not None
    _require(source_snapshot_record.status == "quarantined", "local source snapshot is not quarantined")
    _require(source_snapshot_record.artifact_type == SOURCE_SNAPSHOT_ARTIFACT_TYPE, "local source snapshot type mismatch")
    _require(source_snapshot_record.artifact_id.startswith("PROPOSED_STAGE1_SOURCE_SNAPSHOT__"), "source snapshot ID is not proposal-scoped")
    _require(file_sha256(source_snapshot_record.path) == source_snapshot_record.content_hash, "destination source snapshot bytes were tampered")
    _require(proposal_record.status == "quarantined", "proposal artifact is not quarantined")
    _require(proposal_record.artifact_type == PROPOSAL_ARTIFACT_TYPE, "proposal artifact type mismatch")
    _require(file_sha256(proposal_record.path) == proposal_record.content_hash, "proposal artifact bytes were tampered")
    _require(str(proposal_record.job_id) == destination_registry.job_id, "proposal is not in destination Registry")

    dependency_ids = {item.artifact_id for item in candidate_record.depends_on}
    _require(
        len(candidate_record.depends_on) == 2
        and dependency_ids == {source_snapshot_record.artifact_id, proposal_record.artifact_id},
        "candidate dependencies are incomplete or unexpected",
    )
    _require(source_snapshot_record.job_id == destination_registry.job_id, "source snapshot is not local to destination Registry")
    _require(
        len(proposal_record.depends_on) == 1
        and proposal_record.depends_on[0].artifact_id == source_snapshot_record.artifact_id,
        "proposal artifact does not depend on the local source snapshot",
    )
    snapshot_external_refs = [item for item in source_snapshot_record.depends_on if item.artifact_id]
    _require(len(snapshot_external_refs) == 1, "source snapshot must retain exactly one old authority reference")
    source_ref = snapshot_external_refs[0]

    source_authority = candidate_payload.get("source_authority")
    _require(isinstance(source_authority, Mapping), "candidate source authority is missing")
    source_artifact_id = str(source_authority.get("source_artifact_id") or "")
    source_record = _current_stage1_summary_record(
        source_registry,
        source_artifact_id,
        external_registry_resolver=external_registry_resolver,
    )
    _require(source_record.status == "ready", "original Stage1 source is no longer READY")
    _require(source_record.content_hash == str(source_authority.get("source_artifact_hash") or ""), "original Stage1 source artifact hash changed")
    _require(source_record.job_id == str(source_authority.get("source_job_id") or ""), "original Stage1 source job changed")
    _require(
        source_ref.dependency_kind == "external_job"
        and source_ref.job_id == source_record.job_id
        and source_ref.artifact_id == source_record.artifact_id
        and source_ref.artifact_type == source_record.artifact_type
        and source_ref.content_hash == source_record.content_hash,
        "destination snapshot does not retain the original source authority reference",
    )
    _validate_pointer_source(source_registry, source_record)

    _require(source_snapshot_record.content_hash == source_record.content_hash, "destination source snapshot is not an exact byte copy of the original summary artifact")
    _require(
        source_snapshot_record.metadata.get("source_authority_job_id") == source_record.job_id
        and source_snapshot_record.metadata.get("source_authority_artifact_id") == source_record.artifact_id
        and source_snapshot_record.metadata.get("source_authority_artifact_type") == source_record.artifact_type
        and source_snapshot_record.metadata.get("source_authority_artifact_hash") == source_record.content_hash,
        "destination source snapshot metadata lost its original authority binding",
    )
    _require(
        os.path.normcase(os.path.abspath(source_ref.path))
        == os.path.normcase(os.path.abspath(source_record.path)),
        "destination source snapshot external dependency path mismatch",
    )
    try:
        source_doc = json.loads(Path(source_snapshot_record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceSummaryCorrectionError(f"destination source snapshot is unreadable: {exc}") from exc
    _require(isinstance(source_doc, Mapping), "destination source snapshot payload is invalid")
    source_doc_hash = str(source_doc.get("summary_set_hash") or "")
    source_summaries = source_doc.get("summaries")
    _require(isinstance(source_summaries, list), "destination source snapshot has no summaries list")
    _require(hash_json(source_summaries) == source_doc_hash, "destination source snapshot summary_set_hash mismatch")
    _require(source_doc_hash == str(source_authority.get("source_summary_set_hash") or ""), "destination snapshot summary set changed")

    snapshot_provenance = source_snapshot_record.metadata
    _require(snapshot_provenance.get("usable_as_stage1_reuse") is False, "source snapshot is marked usable as Stage1 input")
    _require(snapshot_provenance.get("source_authority_artifact_hash") == source_record.content_hash, "source snapshot metadata lost old authority hash")

    try:
        proposal_payload = json.loads(Path(proposal_record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceSummaryCorrectionError(f"proposal artifact is unreadable: {exc}") from exc
    _require(isinstance(proposal_payload, Mapping), "proposal artifact payload is invalid")
    _require(proposal_payload.get("artifact_type") == PROPOSAL_ARTIFACT_TYPE, "proposal payload type mismatch")
    _require(proposal_payload.get("proposal_id") == candidate_payload.get("proposal_id"), "candidate and proposal IDs differ")
    raw_proposal = proposal_payload.get("original_proposal_payload")
    _require(isinstance(raw_proposal, Mapping), "normalized proposal has no original proposal payload")
    original_proposal_hash = hash_json(raw_proposal)
    _require(
        original_proposal_hash == str(proposal_payload.get("original_proposal_payload_hash") or "")
        == str(candidate_payload.get("original_proposal_payload_hash") or ""),
        "original proposal payload hash mismatch",
    )
    _, source_summaries, _ = _source_summary_document(source_record)
    source_pdf_info = raw_proposal.get("source_pdf")
    _require(isinstance(source_pdf_info, Mapping), "original proposal has no source PDF binding")
    _, pdf_hash, pdf_page_count = _read_pdf(source_pdf_info.get("path"), source_pdf_info.get("sha256"), source_pdf_info.get("page_count"))
    source_pointer_snapshot = _validate_pointer_source(source_registry, source_record)
    normalized_proposal = _adapt_tripathi_proposal(
        raw_proposal,
        source_record=source_record,
        source_doc=source_doc,
        source_bytes_hash=source_record.content_hash,
        source_pdf_page_count=pdf_page_count,
        source_pointer_snapshot=source_pointer_snapshot,
    )
    normalized_dict = normalized_proposal.to_dict()
    _require(proposal_payload.get("normalized_proposal") == normalized_dict, "stored normalized proposal differs from the source-bound proposal")
    _require(hash_json(normalized_dict) == str(proposal_payload.get("normalized_proposal_hash") or ""), "normalized proposal hash mismatch")
    _require(hash_json(normalized_dict) == str(candidate_payload.get("normalized_proposal_hash") or ""), "candidate proposal hash mismatch")

    expected_candidate_summaries = _apply_field_edits(source_summaries, normalized_proposal)
    normalized_candidate = _validate_candidate_summary_payload(
        candidate_payload,
        source_doc=source_doc,
        source_record=source_record,
        proposal=normalized_proposal,
    )
    _require(normalized_candidate == expected_candidate_summaries, "candidate summaries differ from validated before/after edits")
    _require(_same_pointer(source_registry, source_pointer_snapshot), "source stage1_summaries pointer changed")
    destination_pointer = candidate_payload.get("destination_stage1_pointer_snapshot_before")
    _require(_same_pointer(destination_registry, destination_pointer), "destination stage1_summaries pointer changed")
    _require(pdf_hash == normalized_proposal.source_pdf_sha256, "candidate PDF binding changed")

    return SourceSummaryCorrectionVerificationV1(
        verified=True,
        candidate_artifact_id=candidate_record.artifact_id,
        candidate_status=candidate_record.status,
        source_summary_artifact_id=source_record.artifact_id,
        source_summary_set_hash=normalized_proposal.source_summary_set_hash,
        candidate_summary_set_hash=str(candidate_payload["candidate_summary_set_hash"]),
        identity_set_preserved=bool(candidate_payload.get("identity_set_preserved")),
        source_closure_reverified=True,
        requires_owner_approval=True,
        usable_as_stage1_reuse=False,
        canonical_pointer_advanced=False,
    )


__all__ = [
    "SourceSummaryCorrectionError",
    "SourceSummaryFieldEditV1",
    "SourceSummaryCorrectionProposalV1",
    "SourceSummaryCorrectionPreparationV1",
    "SourceSummaryCorrectionVerificationV1",
    "adapt_tripathi_source_summary_proposal_v1",
    "prepare_source_summary_correction_candidate",
    "verify_source_summary_correction_candidate",
]


