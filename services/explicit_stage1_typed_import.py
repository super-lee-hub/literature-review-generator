"""Resolve explicit typed Stage 1 summaries against a complete PDF bundle."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from runtime import source_intake
from runtime.provider_runtime import hash_json
from runtime.stage_contracts import SourceBundle
from services.artifact_registry import file_sha256
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    Stage1TypedManifestAuthorityV1,
    Stage1TypedManifestAuthorityV2,
    Stage1VisualEvidenceQualificationV1,
    _TypedManifestVerificationScope,
    build_binding_hash,
    verify_stage1_typed_manifest_authority,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_RE = re.compile(
    r"(?:^|\b)(?:dummy|placeholder|not\s+provided|not\s+available|unknown|n/?a|\.\.\.)"
    r"(?:$|\b)",
    re.IGNORECASE,
)
Authority = Stage1TypedManifestAuthorityV1 | Stage1TypedManifestAuthorityV2
RegistryResolver = Callable[[str], Any | None]


class ExplicitTypedImportError(ValueError):
    """Raised when an explicit typed Stage 1 row cannot be imported."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_text(value: Any, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExplicitTypedImportError(f"{label} is not JSON-serializable") from exc


def _resolve_path(value: Any, label: str) -> str:
    raw = _text(value)
    if not raw:
        raise ExplicitTypedImportError(f"{label} is empty")
    try:
        return str(Path(raw).expanduser().resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExplicitTypedImportError(f"{label} cannot be resolved: {raw}") from exc


def _path_key(value: Any, label: str) -> str:
    return os.path.normcase(os.path.normpath(_resolve_path(value, label)))


def _file_hash(path: str, label: str) -> str:
    if not Path(path).is_file():
        raise ExplicitTypedImportError(f"{label} is missing: {path}")
    try:
        digest = file_sha256(path).casefold()
    except (OSError, TypeError, ValueError) as exc:
        raise ExplicitTypedImportError(f"{label} cannot be hashed: {path}") from exc
    if not _SHA256_RE.fullmatch(digest):
        raise ExplicitTypedImportError(f"{label} produced an invalid SHA-256: {path}")
    return digest


def _rows(raw_rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if isinstance(raw_rows, (Mapping, str, bytes, bytearray)):
        raise ExplicitTypedImportError("raw_rows must be a non-empty row sequence")
    try:
        values = list(raw_rows)
    except TypeError as exc:
        raise ExplicitTypedImportError("raw_rows must be a non-empty row sequence") from exc
    if not values:
        raise ExplicitTypedImportError("raw_rows must be non-empty")
    if any(not isinstance(value, Mapping) for value in values):
        bad = next(index for index, value in enumerate(values) if not isinstance(value, Mapping))
        raise ExplicitTypedImportError(f"explicit row index={bad} must be an object")
    return values  # type: ignore[return-value]


def _validate_canonical_summary(row: Mapping[str, Any], key: str) -> None:
    raw = row.get("ai_summary")
    if not isinstance(raw, Mapping):
        raise ExplicitTypedImportError(f"{key}:ai_summary_missing")
    try:
        from runtime.reconcile import validate_canonical_ai_summary

        validate_canonical_ai_summary(raw, label=f"{key}:ai_summary")
    except (TypeError, ValueError) as exc:
        raise ExplicitTypedImportError(f"{key}:ai_summary_schema_invalid:{exc}") from exc
    core = raw.get("core_analysis")
    if not isinstance(core, Mapping):
        raise ExplicitTypedImportError(f"{key}:ai_summary_core_analysis_missing")
    required = ("summary", "methodology", "findings", "conclusions")
    missing = [name for name in required if not _text(core.get(name))]
    if missing:
        raise ExplicitTypedImportError(f"{key}:ai_summary_incomplete:{','.join(missing)}")
    placeholders = [name for name in required if _PLACEHOLDER_RE.search(_text(core.get(name)))]
    if placeholders:
        raise ExplicitTypedImportError(f"{key}:ai_summary_placeholder:{','.join(placeholders)}")


def _row_key(row: Mapping[str, Any], index: int) -> str:
    paper = row.get("paper_info")
    if not isinstance(paper, Mapping):
        raise ExplicitTypedImportError(f"explicit row index={index}:paper_info_missing")
    nested = _text(paper.get("canonical_paper_key"))
    top_level = _text(row.get("canonical_paper_key"))
    if nested and top_level and nested != top_level:
        raise ExplicitTypedImportError(f"explicit row index={index}:canonical_paper_key_conflict")
    key = nested or top_level
    if not key:
        raise ExplicitTypedImportError(f"explicit row index={index}:canonical_paper_key_missing")
    return key


def _row_source_id(row: Mapping[str, Any], binding: Mapping[str, Any], key: str) -> str:
    paper = _mapping(row.get("paper_info"))
    declared = (
        ("paper_info.source_paper_id", _text(paper.get("source_paper_id"))),
        ("row.source_paper_id", _text(row.get("source_paper_id"))),
        ("binding.source_paper_id", _text(binding.get("source_paper_id"))),
    )
    values = {value for _label, value in declared if value}
    if len(values) > 1:
        detail = ",".join(f"{label}={value}" for label, value in declared if value)
        raise ExplicitTypedImportError(f"{key}:source_paper_id_conflict:{detail}")
    source_id = next((value for _label, value in declared if value), "")
    if not source_id:
        raise ExplicitTypedImportError(f"{key}:source_paper_id_missing")
    return source_id


def _bundle_data(bundle: SourceBundle) -> tuple[tuple[Any, ...], dict[str, dict[str, str]]]:
    try:
        bundle.validate()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ExplicitTypedImportError(f"full_bundle validation failed: {exc}") from exc
    items = tuple(bundle.paper_work_items)
    if not items:
        raise ExplicitTypedImportError("full_bundle must contain at least one source item")
    by_key: dict[str, dict[str, str]] = {}
    seen_paths: set[str] = set()
    seen_source_ids: set[str] = set()
    for index, item in enumerate(items):
        key = _text(item.canonical_paper_key)
        source_id = _text(item.source_paper_id)
        if not key:
            raise ExplicitTypedImportError(f"full_bundle item index={index}:canonical_paper_key_missing")
        if not source_id:
            raise ExplicitTypedImportError(f"full_bundle item {key}:source_paper_id_missing")
        path = _resolve_path(item.source_pdf, f"full_bundle item {key}:source_pdf")
        normalized_path = _path_key(path, f"full_bundle item {key}:source_pdf")
        if key in by_key:
            raise ExplicitTypedImportError(f"full_bundle duplicate canonical_paper_key: {key}")
        if normalized_path in seen_paths:
            raise ExplicitTypedImportError(f"full_bundle duplicate source_pdf path: {path}")
        if source_id in seen_source_ids:
            raise ExplicitTypedImportError(f"full_bundle duplicate source_paper_id: {source_id}")
        digest = _file_hash(path, f"full_bundle item {key}:source_pdf")
        descriptor = _mapping(item.source_descriptor)
        paper = _mapping(item.paper_info)
        for declared in (
            descriptor.get("source_pdf_fingerprint"),
            paper.get("source_pdf_fingerprint"),
            descriptor.get("source_pdf_content_sha256"),
            paper.get("source_pdf_content_sha256"),
        ):
            fingerprint = _text(declared).casefold()
            if not fingerprint:
                continue
            if not _SHA256_RE.fullmatch(fingerprint):
                raise ExplicitTypedImportError(f"full_bundle item {key}:source_descriptor_fingerprint_invalid")
            if fingerprint != digest:
                raise ExplicitTypedImportError(f"{key}:source_descriptor_fingerprint_mismatch")
        by_key[key] = {
            "source_paper_id": source_id,
            "source_pdf": path,
            "source_pdf_sha256": digest,
        }
        seen_paths.add(normalized_path)
        seen_source_ids.add(source_id)
    return items, by_key


def _validate_f1(bundle: SourceBundle, binding: Mapping[str, Any]) -> None:
    try:
        selected = source_intake.validate_f1_corpus_source_bundle(bundle, binding=binding)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ExplicitTypedImportError(f"f1_corpus_binding_invalid:{exc}") from exc
    if not isinstance(selected, SourceBundle):
        raise ExplicitTypedImportError("f1_corpus_binding_returned_invalid_bundle")
    expected = {
        _text(item.canonical_paper_key): (
            _path_key(item.source_pdf, "f1 source_pdf"),
            _file_hash(_resolve_path(item.source_pdf, "f1 source_pdf"), "f1 source_pdf"),
        )
        for item in bundle.paper_work_items
    }
    actual = {
        _text(item.canonical_paper_key): (
            _path_key(item.source_pdf, "f1 returned source_pdf"),
            _file_hash(_resolve_path(item.source_pdf, "f1 returned source_pdf"), "f1 returned source_pdf"),
        )
        for item in selected.paper_work_items
    }
    if actual != expected:
        raise ExplicitTypedImportError("f1_corpus_binding_would_trim_or_change_full_bundle")


def _declared_paths(row: Mapping[str, Any], binding: Mapping[str, Any]) -> tuple[str, ...]:
    paper = _mapping(row.get("paper_info"))
    values: list[str] = []
    for value in (
        binding.get("source_pdf"),
        binding.get("original_source_location"),
        binding.get("current_source_location"),
        _mapping(binding.get("extra")).get("source_pdf"),
        _mapping(binding.get("extra")).get("source_pdf_path"),
        _mapping(binding.get("extra")).get("source_locator"),
        paper.get("source_pdf"),
        paper.get("pdf_path"),
        row.get("source_pdf"),
        row.get("pdf_path"),
    ):
        text = _text(value)
        if text and text not in values:
            values.append(text)
    return tuple(values)


def _declared_hash(row: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    paper = _mapping(row.get("paper_info"))
    declarations = (
        ("binding.source_pdf_content_sha256", binding.get("source_pdf_content_sha256")),
        ("binding.source_pdf_fingerprint", binding.get("source_pdf_fingerprint")),
        ("binding.extra.source_pdf_file_hash", _mapping(binding.get("extra")).get("source_pdf_file_hash")),
        ("paper_info.source_pdf_content_sha256", paper.get("source_pdf_content_sha256")),
        ("paper_info.source_pdf_fingerprint", paper.get("source_pdf_fingerprint")),
        ("row.source_pdf_content_sha256", row.get("source_pdf_content_sha256")),
        ("row.source_pdf_fingerprint", row.get("source_pdf_fingerprint")),
    )
    values: list[tuple[str, str]] = []
    for label, raw in declarations:
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ExplicitTypedImportError(f"source_pdf hash declaration is not a string: {label}")
        digest = raw.strip()
        if not digest:
            continue
        if not _SHA256_RE.fullmatch(digest):
            raise ExplicitTypedImportError(f"source_pdf hash declaration is invalid: {label}")
        values.append((label, digest))
    distinct = {digest for _label, digest in values}
    if len(distinct) > 1:
        labels = ",".join(label for label, _digest in values)
        raise ExplicitTypedImportError(
            f"source_pdf_content_sha256_mismatch:source_pdf hash declarations conflict: {labels}"
        )
    return values[0][1] if values else ""


def _check_source_join(
    row: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    key: str,
    source_id: str,
    expected: Mapping[str, str],
) -> None:
    if source_id != expected["source_paper_id"]:
        raise ExplicitTypedImportError(f"{key}:source_paper_id_mismatch:{source_id}")
    expected_path = _path_key(expected["source_pdf"], f"{key}:current source_pdf")
    paths = _declared_paths(row, binding)
    if not paths:
        raise ExplicitTypedImportError(f"{key}:source_pdf_locator_missing")
    if any(_path_key(path, f"{key}:old source_pdf") != expected_path for path in paths):
        raise ExplicitTypedImportError(f"{key}:source_pdf_path_mismatch")
    digest = _declared_hash(row, binding)
    if not digest:
        raise ExplicitTypedImportError(f"{key}:source_pdf_content_sha256_missing")
    if not _SHA256_RE.fullmatch(digest):
        raise ExplicitTypedImportError(f"{key}:source_pdf_content_sha256_invalid")
    if digest != expected["source_pdf_sha256"]:
        raise ExplicitTypedImportError(f"{key}:source_pdf_content_sha256_mismatch")
    paper = _mapping(row.get("paper_info"))
    for value in (paper.get("source_pdf_fingerprint"), paper.get("source_pdf_content_sha256")):
        declared = _text(value).casefold()
        if declared and declared != expected["source_pdf_sha256"]:
            raise ExplicitTypedImportError(f"{key}:source_pdf_fingerprint_mismatch")


def _authority_refs(binding: Stage1ReusableSummaryBindingV1, authority: Authority) -> dict[str, Any]:
    manifest = authority.manifest
    closure_id = _text(binding.source_provider_receipt_closure_id or manifest.provider_receipt_closure_id)
    closure_hash = _text(binding.source_provider_receipt_closure_hash or manifest.provider_receipt_closure_hash)
    ledger_id = _text(binding.source_provider_receipt_ledger_id or manifest.provider_receipt_ledger_id)
    ledger_hash = _text(binding.source_provider_receipt_ledger_hash or manifest.provider_receipt_ledger_hash)
    owner_refs: list[dict[str, str]] = []
    owner = _mapping(getattr(manifest, "owner_correction_authority", {}))
    names = ("adoption_receipt", "candidate", "correction_registry", "derived_summary_set", "origin_prior_manifest", "origin_registry", "origin_source", "proposal")
    for name in sorted(names):
        value = _mapping(owner.get(name))
        if value:
            owner_refs.append({
                "name": name,
                "job_id": _text(value.get("job_id")),
                "artifact_id": _text(value.get("artifact_id")),
                "content_hash": _text(value.get("content_hash")),
                "path": _text(value.get("path")),
            })
    return {
        "typed_manifest_artifact_id": authority.manifest_artifact_id,
        "typed_manifest_artifact_hash": authority.manifest_file_hash,
        "typed_manifest_path": authority.manifest_path,
        "typed_manifest_content_hash": _text(manifest.manifest_content_hash),
        "source_summary_artifact_id": _text(manifest.source_summary_artifact_id),
        "source_summary_artifact_hash": _text(manifest.source_summary_artifact_hash),
        "source_summary_path": authority.source_summary_path,
        "source_provider_receipt_closure_id": closure_id,
        "source_provider_receipt_closure_hash": closure_hash,
        "source_provider_receipt_closure_path": authority.provider_closure_path,
        "source_provider_receipt_ledger_id": ledger_id,
        "source_provider_receipt_ledger_hash": ledger_hash,
        "source_provider_receipt_ledger_path": authority.provider_ledger_path,
        "source_authority_job_id": _text(manifest.job_id),
        "source_authority_registry_id": _text(binding.source_authority_registry_id or manifest.source_registry_identity),
        "source_authority_registry_revision": _text(binding.source_authority_registry_revision or manifest.source_registry_revision),
        "owner_correction_refs": owner_refs,
    }


def _identity(
    *,
    key: str,
    source_id: str,
    source_pdf: Mapping[str, str],
    binding: Stage1ReusableSummaryBindingV1,
    authority: Authority,
) -> dict[str, Any]:
    refs = _authority_refs(binding, authority)
    manifest = authority.manifest
    return {
        "canonical_paper_key": key,
        "source_paper_id": source_id,
        "current_source_pdf_path": source_pdf["source_pdf"],
        "current_source_pdf_sha256": source_pdf["source_pdf_sha256"],
        **refs,
        "original_summary_payload_hash": _text(manifest.summary_payload_hash),
        "original_binding_hash": build_binding_hash(binding.to_dict()),
    }


def _historical_transport(row: Mapping[str, Any], binding: Stage1ReusableSummaryBindingV1) -> tuple[int, tuple[str, ...]]:
    provider = _mapping(row.get("provider"))
    extra = _mapping(binding.extra)
    raw_count = extra.get("provider_transport_count")
    if raw_count in (None, ""):
        raw_count = provider.get("transport_count")
    receipt_ids = tuple(_text(value) for value in (provider.get("receipt_ids") or ()) if _text(value))
    try:
        count = int(raw_count) if raw_count not in (None, "") else len(receipt_ids)
    except (TypeError, ValueError):
        count = len(receipt_ids)
    return max(0, count), receipt_ids


def _source_item(
    *,
    identity: Mapping[str, Any],
    source_id: str,
    source_pdf: Mapping[str, str],
    binding: Stage1ReusableSummaryBindingV1,
    authority: Authority,
    historical_count: int,
    historical_receipts: Sequence[str],
) -> dict[str, Any]:
    refs = _authority_refs(binding, authority)
    original_manifest = {
        "artifact_id": refs["typed_manifest_artifact_id"],
        "content_hash": refs["typed_manifest_artifact_hash"],
        "path": refs["typed_manifest_path"],
        "manifest_content_hash": refs["typed_manifest_content_hash"],
    }
    original_source = {
        "artifact_id": refs["source_summary_artifact_id"],
        "content_hash": refs["source_summary_artifact_hash"],
        "path": refs["source_summary_path"],
    }
    original_closure = {
        "artifact_id": refs["source_provider_receipt_closure_id"],
        "content_hash": refs["source_provider_receipt_closure_hash"],
        "path": refs["source_provider_receipt_closure_path"],
    }
    original_ledger = {
        "artifact_id": refs["source_provider_receipt_ledger_id"],
        "content_hash": refs["source_provider_receipt_ledger_hash"],
        "path": refs["source_provider_receipt_ledger_path"],
    }
    return {
        "canonical_paper_key": identity["canonical_paper_key"],
        "source_paper_id": source_id,
        "source_pdf": source_pdf["source_pdf"],
        "source_pdf_sha256": source_pdf["source_pdf_sha256"],
        "disposition": "typed_imported",
        "authority_kind": "typed_manifest",
        "provider_receipt_ids": [],
        "current_transport_count": 0,
        "original_provider_transport_count": historical_count,
        "original_provider_receipt_ids": list(historical_receipts),
        "current_pdf_join": {
            "canonical_paper_key": identity["canonical_paper_key"],
            "source_paper_id": source_id,
            "path": source_pdf["source_pdf"],
            "sha256": source_pdf["source_pdf_sha256"],
        },
        "original_typed_manifest": original_manifest,
        "original_source_summary": original_source,
        "original_provider_receipt_closure": original_closure,
        "original_provider_receipt_ledger": original_ledger,
        "original_summary_payload_hash": identity["original_summary_payload_hash"],
        "original_binding_hash": identity["original_binding_hash"],
        "typed_manifest_artifact_id": refs["typed_manifest_artifact_id"],
        "typed_manifest_artifact_hash": refs["typed_manifest_artifact_hash"],
    }


@dataclass(frozen=True)
class ExplicitTypedImportEntry:
    paper_key: str
    source_paper_id: str
    current_source_pdf_path: str
    current_source_pdf_sha256: str
    authority: Authority
    _summary_json: str = field(repr=False)
    _binding_json: str = field(repr=False)
    _source_item_json: str = field(repr=False)
    _identity_json: str = field(repr=False)

    @property
    def summary(self) -> dict[str, Any]:
        value = json.loads(self._summary_json)
        if not isinstance(value, dict):
            raise ExplicitTypedImportError(f"{self.paper_key}:stored summary is not an object")
        return value

    @property
    def binding(self) -> Stage1ReusableSummaryBindingV1:
        value = json.loads(self._binding_json)
        if not isinstance(value, Mapping):
            raise ExplicitTypedImportError(f"{self.paper_key}:stored binding is not an object")
        return Stage1ReusableSummaryBindingV1.from_mapping(value)

    def source_item(self) -> dict[str, Any]:
        value = json.loads(self._source_item_json)
        if not isinstance(value, dict):
            raise ExplicitTypedImportError(f"{self.paper_key}:stored source item is not an object")
        return value

    def identity(self) -> dict[str, Any]:
        value = json.loads(self._identity_json)
        if not isinstance(value, dict):
            raise ExplicitTypedImportError(f"{self.paper_key}:stored identity is not an object")
        return value


@dataclass(frozen=True)
class ExplicitTypedImportPlan:
    imports: tuple[ExplicitTypedImportEntry, ...]
    pending_keys: tuple[str, ...]
    authority_set_hash: str
    _full_bundle_json: str = field(repr=False)

    @property
    def full_bundle(self) -> SourceBundle:
        value = json.loads(self._full_bundle_json)
        if not isinstance(value, Mapping):
            raise ExplicitTypedImportError("stored full bundle is not an object")
        return SourceBundle.from_dict(value)


def _resolve_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    items: Sequence[Any],
    bundle_by_key: Mapping[str, Mapping[str, str]],
    external_registry_resolver: RegistryResolver | None,
    verification_scope: _TypedManifestVerificationScope | None,
    require_complete_visual_coverage: bool,
) -> tuple[ExplicitTypedImportEntry, ...]:
    entries: dict[str, ExplicitTypedImportEntry] = {}
    source_ids: set[str] = set()
    for index, row in enumerate(rows):
        key = _row_key(row, index)
        expected = bundle_by_key.get(key)
        if expected is None:
            raise ExplicitTypedImportError(f"explicit row {key}:unknown_canonical_paper_key")
        if key in entries:
            raise ExplicitTypedImportError(f"{key}:duplicate_explicit_row")
        if _text(row.get("status")).casefold() != "success":
            raise ExplicitTypedImportError(f"{key}:status_not_success")
        _validate_canonical_summary(row, key)
        reuse = row.get("stage1_reuse")
        if not isinstance(reuse, Mapping):
            raise ExplicitTypedImportError(f"{key}:stage1_reuse_missing")
        if _text(reuse.get("authority_kind")) != "typed_manifest":
            raise ExplicitTypedImportError(f"{key}:stage1_reuse_authority_kind_invalid")
        raw_binding = reuse.get("binding")
        if not isinstance(raw_binding, Mapping) or not raw_binding:
            raise ExplicitTypedImportError(f"{key}:stage1_reuse_binding_missing")
        source_id = _row_source_id(row, raw_binding, key)
        if source_id in source_ids:
            raise ExplicitTypedImportError(f"{key}:duplicate_explicit_source_paper_id:{source_id}")
        if _text(raw_binding.get("canonical_paper_key")) != key:
            raise ExplicitTypedImportError(f"{key}:binding_canonical_paper_key_mismatch")
        _check_source_join(row, raw_binding, key=key, source_id=source_id, expected=expected)
        try:
            binding = Stage1ReusableSummaryBindingV1.from_mapping(raw_binding)
        except (TypeError, ValueError) as exc:
            raise ExplicitTypedImportError(f"{key}:binding_invalid:{exc}") from exc
        try:
            authority, reason = verify_stage1_typed_manifest_authority(
                row,
                binding,
                external_registry_resolver=external_registry_resolver,
                _verification_scope=verification_scope,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ExplicitTypedImportError(f"{key}:typed_manifest_verification_error:{exc}") from exc
        if authority is None:
            raise ExplicitTypedImportError(f"{key}:typed_manifest_authority_rejected:{reason}")
        manifest = authority.manifest
        if _text(manifest.canonical_paper_key) != key:
            raise ExplicitTypedImportError(f"{key}:manifest_canonical_paper_key_mismatch")
        if _text(manifest.source_paper_id) and _text(manifest.source_paper_id) != source_id:
            raise ExplicitTypedImportError(f"{key}:manifest_source_paper_id_mismatch")
        summary_hash = hash_json(_mapping(row.get("ai_summary")))
        if not _text(manifest.summary_payload_hash) or summary_hash != _text(manifest.summary_payload_hash):
            raise ExplicitTypedImportError(f"{key}:typed_manifest_summary_payload_hash_mismatch")
        if require_complete_visual_coverage:
            raw_qualification = getattr(manifest, "visual_evidence_qualification", {})
            if not isinstance(raw_qualification, Mapping) or not raw_qualification:
                raise ExplicitTypedImportError(f"{key}:visual_evidence_qualification_missing_for_complete_import")
            try:
                qualification = Stage1VisualEvidenceQualificationV1.from_current_mapping_strict(raw_qualification)
            except (TypeError, ValueError) as exc:
                raise ExplicitTypedImportError(f"{key}:visual_evidence_qualification_invalid:{exc}") from exc
            if not qualification.require_complete_visual_coverage:
                raise ExplicitTypedImportError(f"{key}:visual_evidence_qualification_policy_relaxed")
            if not qualification.complete_for_reuse():
                raise ExplicitTypedImportError(f"{key}:visual_evidence_qualification_incomplete")
        source_pdf = {"source_pdf": expected["source_pdf"], "source_pdf_sha256": expected["source_pdf_sha256"]}
        identity = _identity(
            key=key,
            source_id=source_id,
            source_pdf=source_pdf,
            binding=binding,
            authority=authority,
        )
        historical_count, historical_receipts = _historical_transport(row, binding)
        source_item = _source_item(
            identity=identity,
            source_id=source_id,
            source_pdf=source_pdf,
            binding=binding,
            authority=authority,
            historical_count=historical_count,
            historical_receipts=historical_receipts,
        )
        entries[key] = ExplicitTypedImportEntry(
            paper_key=key,
            source_paper_id=source_id,
            current_source_pdf_path=expected["source_pdf"],
            current_source_pdf_sha256=expected["source_pdf_sha256"],
            authority=authority,
            _summary_json=_json_text(row, f"{key}:summary_row"),
            _binding_json=_json_text(raw_binding, f"{key}:binding"),
            _source_item_json=_json_text(source_item, f"{key}:source_item"),
            _identity_json=_json_text(identity, f"{key}:identity"),
        )
        source_ids.add(source_id)
    return tuple(entries[_text(item.canonical_paper_key)] for item in items if _text(item.canonical_paper_key) in entries)


def resolve_explicit_typed_imports(
    raw_rows: Iterable[Mapping[str, Any]],
    full_bundle: SourceBundle,
    *,
    f1_binding: Mapping[str, Any] | None = None,
    external_registry_resolver: RegistryResolver | None = None,
    _verification_scope: _TypedManifestVerificationScope | None = None,
    require_complete_visual_coverage: bool = True,
) -> ExplicitTypedImportPlan:
    """Resolve explicit typed imports while retaining every current source."""

    if not isinstance(full_bundle, SourceBundle):
        raise ExplicitTypedImportError("full_bundle must be a SourceBundle")
    items, bundle_by_key = _bundle_data(full_bundle)
    if f1_binding is not None:
        if not isinstance(f1_binding, Mapping):
            raise ExplicitTypedImportError("f1_binding must be a mapping")
        _validate_f1(full_bundle, f1_binding)
    values = _rows(raw_rows)
    if _verification_scope is None:
        try:
            with _TypedManifestVerificationScope() as scope:
                imports = _resolve_rows(
                    values,
                    items=items,
                    bundle_by_key=bundle_by_key,
                    external_registry_resolver=external_registry_resolver,
                    verification_scope=scope,
                    require_complete_visual_coverage=require_complete_visual_coverage,
                )
        except ExplicitTypedImportError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ExplicitTypedImportError(f"typed_manifest_verification_scope_failed:{exc}") from exc
    else:
        imports = _resolve_rows(
            values,
            items=items,
            bundle_by_key=bundle_by_key,
            external_registry_resolver=external_registry_resolver,
            verification_scope=_verification_scope,
            require_complete_visual_coverage=require_complete_visual_coverage,
        )
    imported = {entry.paper_key for entry in imports}
    pending = tuple(_text(item.canonical_paper_key) for item in items if _text(item.canonical_paper_key) not in imported)
    authority_set_hash = hash_json({"schema": "explicit-typed-import-authority-set/v1", "entries": [entry.identity() for entry in imports]})
    return ExplicitTypedImportPlan(
        imports=imports,
        pending_keys=pending,
        authority_set_hash=authority_set_hash,
        _full_bundle_json=_json_text(full_bundle.to_dict(), "full_bundle"),
    )


__all__ = [
    "ExplicitTypedImportError",
    "ExplicitTypedImportEntry",
    "ExplicitTypedImportPlan",
    "resolve_explicit_typed_imports",
]
