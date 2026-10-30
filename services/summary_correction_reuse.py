"""Owner-corrected Stage 1 manifest export over an approved correction receipt.

This module is a narrow adapter from the quarantined correction workflow to a
versioned Stage 1 reuse authority.  It never creates owner approval, edits the
origin Registry, or manufactures the prior Stage 1 execution basis.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from runtime.provider_runtime import hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.stage1_reuse import (
    OWNER_APPROVED_SOURCE_CORRECTION_KIND,
    Stage1OwnerCorrectionAuthorityV1,
    Stage1ReusableSummaryBindingV1,
    Stage1ReusableSummaryManifestV2,
    Stage1TypedManifestAuthorityV2,
    build_binding_hash,
    build_owner_corrected_stage1_binding,
    verify_stage1_typed_manifest_authority,
)
from services.summary_correction_adoption import (
    ADOPTION_RECEIPT_ARTIFACT_TYPE,
    ADOPTION_RECEIPT_ARTIFACT_VERSION,
    DERIVED_SUMMARY_SET_ARTIFACT_TYPE,
    DERIVED_SUMMARY_SET_ARTIFACT_VERSION,
    SourceSummaryCorrectionAdoptionError,
    verify_source_summary_correction_adoption,
)


OWNER_CORRECTED_IMPORT_BUNDLE_TYPE = "stage1_owner_corrected_reuse_import_bundle"
OWNER_CORRECTED_IMPORT_BUNDLE_VERSION = "v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class OwnerCorrectedStage1ReuseExportError(ValueError):
    """Raised when the approved correction lacks an exact reusable basis."""


@dataclass(frozen=True)
class OwnerCorrectedStage1ReuseExportResultV1:
    status: str
    action_id: str
    target_canonical_paper_key: str
    summary_file_artifact_id: str
    summary_file_artifact_hash: str
    summary_file_path: str
    owner_corrected_manifest_artifact_id: str
    owner_corrected_manifest_artifact_hash: str
    owner_corrected_manifest_path: str
    owner_import_bundle_artifact_id: str
    owner_import_bundle_artifact_hash: str
    owner_import_bundle_path: str
    paper_count: int
    unchanged_prior_manifest_count: int
    manifest_refs: tuple[Mapping[str, Any], ...]
    origin_registry_unchanged: bool
    usable_as_stage1_reuse: bool = True
    canonical_pointer_advanced: bool = False
    provider_calls: int = 0
    provider_receipt_ids_created: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["manifest_refs"] = [dict(item) for item in self.manifest_refs]
        payload["provider_receipt_ids_created"] = list(self.provider_receipt_ids_created)
        return payload


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OwnerCorrectedStage1ReuseExportError(message)


def _require_mapping(value: object, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OwnerCorrectedStage1ReuseExportError(message)
    return value


def _require_record(record: ArtifactRecord | None, message: str) -> ArtifactRecord:
    if record is None:
        raise OwnerCorrectedStage1ReuseExportError(message)
    return record


def _require_rows(value: object, message: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise OwnerCorrectedStage1ReuseExportError(message)
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise OwnerCorrectedStage1ReuseExportError(
                f"{message}: entry {index} is not an object"
            )
        rows.append(copy.deepcopy(dict(row)))
    return rows


def _read_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OwnerCorrectedStage1ReuseExportError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, Mapping):
        raise OwnerCorrectedStage1ReuseExportError(f"{label} must be an object")
    return dict(value)


def _read_json_rows(path: str | Path, *, label: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OwnerCorrectedStage1ReuseExportError(f"{label} is unreadable: {exc}") from exc
    return _require_rows(value, f"{label} must be a JSON array")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _artifact_ref(record: ArtifactRecord) -> dict[str, str]:
    return {
        "job_id": record.job_id,
        "artifact_id": record.artifact_id,
        "artifact_role": record.artifact_role,
        "artifact_type": record.artifact_type,
        "artifact_version": record.artifact_version,
        "status": record.status,
        "path": str(Path(record.path).resolve()),
        "content_hash": record.content_hash,
    }


def _resolve_registries(
    origin_registry: ArtifactRegistry,
    correction_registry: ArtifactRegistry,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> Callable[[str], ArtifactRegistry | None]:
    def resolve(job_id: str) -> ArtifactRegistry | None:
        if job_id == origin_registry.job_id:
            return origin_registry
        if job_id == correction_registry.job_id:
            return correction_registry
        if external_registry_resolver is None:
            return None
        return external_registry_resolver(job_id)

    return resolve


def _resolve_manifest_registry(
    locator: Mapping[str, Any],
    *,
    label: str,
    resolver: Callable[[str], ArtifactRegistry | None],
) -> ArtifactRegistry:
    job_id = str(locator.get("job_id") or "").strip()
    registry_path = str(locator.get("registry_path") or "").strip()
    if not job_id or not registry_path:
        raise OwnerCorrectedStage1ReuseExportError(f"{label} Registry locator is incomplete")
    registry = resolver(job_id)
    if registry is None:
        raise OwnerCorrectedStage1ReuseExportError(f"{label} Registry is not authorized")
    resolved_path = os.path.normcase(os.path.abspath(registry.registry_path))
    expected_path = os.path.normcase(os.path.abspath(registry_path))
    if registry.job_id != job_id or resolved_path != expected_path:
        raise OwnerCorrectedStage1ReuseExportError(
            f"{label} Registry resolver result does not match the declared locator"
        )
    registry.reload()
    return registry


def _paper_key(summary: Mapping[str, Any]) -> str:
    paper_info = summary.get("paper_info")
    if not isinstance(paper_info, Mapping):
        return ""
    return str(paper_info.get("canonical_paper_key") or "").strip()


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _summary_map(rows: Sequence[Mapping[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        key = _paper_key(row)
        if not key:
            raise OwnerCorrectedStage1ReuseExportError(
                f"{label} row {index} has no canonical_paper_key"
            )
        if key in by_key:
            raise OwnerCorrectedStage1ReuseExportError(
                f"{label} contains duplicate paper identity: {key}"
            )
        if not isinstance(row.get("ai_summary"), Mapping):
            raise OwnerCorrectedStage1ReuseExportError(
                f"{label} row {key} has no ai_summary object"
            )
        by_key[key] = dict(row)
    return by_key


def _summary_file_projection(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Add the per-row hash declaration required by summary_file authority."""

    projected: list[dict[str, Any]] = []
    for index, summary in enumerate(rows):
        ai_summary = summary.get("ai_summary")
        if not isinstance(ai_summary, Mapping):
            raise OwnerCorrectedStage1ReuseExportError(
                f"summary_file row {index} has no ai_summary object"
            )
        summary_payload_hash = hash_json(ai_summary)
        existing_hash = summary.get("summary_payload_hash")
        if existing_hash is not None and str(existing_hash) != summary_payload_hash:
            raise OwnerCorrectedStage1ReuseExportError(
                f"summary_file row {index} has a conflicting summary_payload_hash"
            )
        row = copy.deepcopy(dict(summary))
        row["summary_payload_hash"] = summary_payload_hash
        projected.append(row)
    return projected


def _manifest_binding_hash(payload: Mapping[str, Any]) -> str:
    declared = str(payload.get("binding_hash") or "")
    binding = payload.get("binding")
    if not isinstance(binding, Mapping) or not _sha256(declared):
        return ""
    return build_binding_hash(binding)


def _verify_prior_manifest_record(
    *,
    origin_registry: ArtifactRegistry,
    origin_source_record: ArtifactRecord,
    source_summary: Mapping[str, Any],
    external_registry_resolver: Callable[[str], ArtifactRegistry | None],
) -> tuple[ArtifactRecord, dict[str, Any], Stage1ReusableSummaryBindingV1]:
    """Resolve one exact registered V1 manifest as the prior Stage 1 basis."""

    key = _paper_key(source_summary)
    ai_summary = source_summary.get("ai_summary")
    if not key or not isinstance(ai_summary, Mapping):
        raise OwnerCorrectedStage1ReuseExportError(
            "prior Stage 1 summary lacks a canonical identity or ai_summary"
        )
    ai_summary_hash = hash_json(ai_summary)
    row_reuse = source_summary.get("stage1_reuse")
    row_binding: Stage1ReusableSummaryBindingV1 | None = None
    if isinstance(row_reuse, Mapping) and isinstance(row_reuse.get("binding"), Mapping):
        row_binding = Stage1ReusableSummaryBindingV1.from_mapping(row_reuse["binding"])
    matches: list[tuple[ArtifactRecord, dict[str, Any], Stage1ReusableSummaryBindingV1]] = []
    for record in origin_registry.list_records():
        if (
            record.status != "ready"
            or record.artifact_type != "stage1_reusable_summary_manifest"
            or record.artifact_version != "v1"
        ):
            continue
        if file_sha256(record.path) != record.content_hash:
            continue
        try:
            payload = _read_json_object(record.path, label="prior Stage 1 reusable manifest")
        except OwnerCorrectedStage1ReuseExportError:
            continue
        summary_payload = payload.get("summary_payload")
        raw_binding = payload.get("binding")
        if not isinstance(raw_binding, Mapping):
            continue
        if (
            payload.get("artifact_type") != "stage1_reusable_summary_manifest"
            or payload.get("artifact_version") != "v1"
            or str(payload.get("canonical_paper_key") or "") != key
            or not isinstance(summary_payload, Mapping)
            or hash_json(summary_payload) != ai_summary_hash
            or str(payload.get("source_summary_artifact_id") or "")
            != str(raw_binding.get("source_authority_artifact_id") or "")
            or str(payload.get("source_summary_artifact_hash") or "")
            != str(raw_binding.get("source_authority_artifact_hash") or "")
        ):
            continue
        binding = Stage1ReusableSummaryBindingV1.from_mapping(raw_binding)
        if row_binding is not None and build_binding_hash(row_binding.to_dict()) != str(
            payload.get("binding_hash") or ""
        ):
            continue
        try:
            origin_registry.verify_ready_artifact_closure(
                record,
                external_registry_resolver=external_registry_resolver,
            )
        except (OSError, RegistryError, ValueError, TypeError):
            continue
        prior_summary = copy.deepcopy(dict(source_summary))
        prior_summary["stage1_reuse"] = {
            "authority_kind": "typed_manifest",
            "typed_manifest_path": str(Path(record.path).resolve()),
            "typed_manifest_artifact_id": record.artifact_id,
            "typed_manifest_artifact_hash": record.content_hash,
            "binding": dict(raw_binding),
        }
        authority, _reason = verify_stage1_typed_manifest_authority(
            prior_summary,
            binding,
            external_registry_resolver=external_registry_resolver,
        )
        if authority is None or authority.manifest.artifact_version != "v1":
            continue
        matches.append((record, payload, binding))
    if len(matches) != 1:
        reason = "missing" if not matches else "ambiguous"
        raise OwnerCorrectedStage1ReuseExportError(
            f"prior registered Stage 1 V1 manifest is {reason} for {key}"
        )
    return matches[0]


def _source_binding_projection(
    binding: Stage1ReusableSummaryBindingV1,
) -> dict[str, Any]:
    payload = binding.to_dict()
    payload.pop("source_summary_manifest_id", None)
    payload.pop("source_summary_manifest_hash", None)
    return payload


def _assert_owner_binding_preserves_prior_basis(
    *,
    prior: Stage1ReusableSummaryBindingV1,
    corrected: Stage1ReusableSummaryBindingV1,
) -> None:
    allowed_changes = {
        "source_kind",
        "normalized_summary_payload_hash",
        "summary_payload_hash",
        "provider_receipt_closure_id",
        "provider_receipt_closure_hash",
        "source_provider_receipt_closure_id",
        "source_provider_receipt_closure_hash",
        "source_provider_receipt_ledger_id",
        "source_provider_receipt_ledger_hash",
        "registered_source_artifact_id",
        "registered_source_artifact_hash",
        "registered_source_artifact_path",
        "registry_file_hash",
        "source_summary_manifest_id",
        "source_summary_manifest_hash",
        "source_authority_job_id",
        "source_authority_artifact_id",
        "source_authority_artifact_hash",
        "source_authority_artifact_path",
        "source_authority_registry_id",
        "source_authority_registry_revision",
        "source_authority_closure_id",
        "source_authority_closure_hash",
        "source_authority_registry_path",
        "extra",
    }
    for field_name in Stage1ReusableSummaryBindingV1.__dataclass_fields__:
        if field_name in allowed_changes:
            continue
        if getattr(prior, field_name) != getattr(corrected, field_name):
            raise OwnerCorrectedStage1ReuseExportError(
                f"owner-corrected binding changed prior Stage 1 basis field: {field_name}"
            )
    prior_extra = dict(prior.extra)
    corrected_extra = dict(corrected.extra)
    for mutable_key in ("source_kind", "provider_transport_count"):
        prior_extra.pop(mutable_key, None)
        corrected_extra.pop(mutable_key, None)
    if prior_extra != corrected_extra:
        raise OwnerCorrectedStage1ReuseExportError(
            "owner-corrected binding changed prior Stage 1 evidence in extra"
        )


def _publish_or_verify(
    *,
    publication_context: Any,
    registry: ArtifactRegistry,
    workspace: JobWorkspace,
    path: str,
    payload: Any,
    artifact_id: str,
    artifact_role: str,
    artifact_type: str,
    artifact_version: str,
    producer: str,
    dependencies: Sequence[ArtifactDependencyRefV2],
    external_registry_resolver: Callable[[str], ArtifactRegistry | None],
    metadata: Mapping[str, Any],
) -> ArtifactRecord:
    expected_hash = hashlib.sha256(_json_bytes(payload)).hexdigest()
    registry.reload()
    existing = registry.get(artifact_id)
    if existing is not None:
        if (
            existing.status != "ready"
            or existing.artifact_type != artifact_type
            or existing.artifact_version != artifact_version
            or existing.content_hash != expected_hash
            or file_sha256(existing.path) != expected_hash
            or [ref.to_dict() for ref in existing.depends_on]
            != [ref.to_dict() for ref in dependencies]
        ):
            raise OwnerCorrectedStage1ReuseExportError(
                f"existing owner-correction artifact conflicts with requested bytes: {artifact_id}"
            )
        registry.verify_ready_artifact_closure(
            existing,
            external_registry_resolver=external_registry_resolver,
        )
        return existing
    record = publish_json_artifact(
        publication_context,
        registry,
        path,
        payload,
        artifact_id=artifact_id,
        artifact_role=artifact_role,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        producer=producer,
        status="ready",
        depends_on=list(dependencies),
        external_registry_resolver=external_registry_resolver,
        metadata=dict(metadata),
    )
    if record.content_hash != expected_hash or file_sha256(record.path) != expected_hash:
        raise OwnerCorrectedStage1ReuseExportError(
            f"published owner-correction artifact bytes changed: {artifact_id}"
        )
    registry.verify_ready_artifact_closure(
        record,
        external_registry_resolver=external_registry_resolver,
    )
    return record


def _ensure_source_file_revision(
    registry: ArtifactRegistry,
    record: ArtifactRecord,
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None],
) -> tuple[ArtifactRecord, str]:
    """Freeze the Registry revision at which the source file was registered."""

    registry.reload()
    current = _require_record(registry.get(record.artifact_id), "owner summary_file disappeared")
    revision = str(current.metadata.get("source_authority_registry_revision") or "")
    if revision:
        return current, revision
    before_update = registry.revision
    try:
        updated = registry.update_record(
            current.artifact_id,
            external_registry_resolver=external_registry_resolver,
            metadata_updates={"source_authority_registry_revision": str(before_update)},
            expected_revision=before_update,
        )
    except (OSError, RegistryError, ValueError, TypeError, RuntimeError):
        registry.reload()
        current = _require_record(
            registry.get(record.artifact_id), "owner summary_file disappeared during revision binding"
        )
        revision = str(current.metadata.get("source_authority_registry_revision") or "")
        if not revision:
            raise
        return current, revision
    return updated, str(before_update)


def _workspace_registry_checks(
    *,
    origin_registry: ArtifactRegistry,
    correction_registry: ArtifactRegistry,
    workspace: JobWorkspace,
) -> None:
    if workspace.job_id != correction_registry.job_id:
        raise OwnerCorrectedStage1ReuseExportError(
            "correction workspace and Registry job_id differ"
        )
    workspace_path = os.path.normcase(os.path.abspath(workspace.paths.registry_path))
    correction_path = os.path.normcase(os.path.abspath(correction_registry.registry_path))
    origin_path = os.path.normcase(os.path.abspath(origin_registry.registry_path))
    if workspace_path != correction_path:
        raise OwnerCorrectedStage1ReuseExportError(
            "correction Registry does not belong to the correction workspace"
        )
    if origin_path == correction_path or origin_registry.job_id == correction_registry.job_id:
        raise OwnerCorrectedStage1ReuseExportError(
            "owner correction export requires separate origin and correction Registries"
        )


def _prior_manifest_import_summary(
    source_summary: Mapping[str, Any],
    *,
    manifest_record: ArtifactRecord,
    manifest_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], Stage1ReusableSummaryBindingV1]:
    raw_binding = manifest_payload.get("binding")
    if not isinstance(raw_binding, Mapping):
        raise OwnerCorrectedStage1ReuseExportError("prior V1 manifest has no typed binding")
    binding = Stage1ReusableSummaryBindingV1.from_mapping(raw_binding)
    prior_summary = copy.deepcopy(dict(source_summary))
    prior_summary["stage1_reuse"] = {
        "authority_kind": "typed_manifest",
        "typed_manifest_path": str(Path(manifest_record.path).resolve()),
        "typed_manifest_artifact_id": manifest_record.artifact_id,
        "typed_manifest_artifact_hash": manifest_record.content_hash,
        "binding": dict(raw_binding),
    }
    return prior_summary, binding


def _owner_corrected_manifest_payload(
    *,
    prior_manifest: Mapping[str, Any],
    prior_binding: Stage1ReusableSummaryBindingV1,
    summary_file: ArtifactRecord,
    source_registry_revision: str,
    target_summary: Mapping[str, Any],
    owner_authority: Stage1OwnerCorrectionAuthorityV1,
    correction_job_id: str,
) -> dict[str, Any]:
    for id_name, hash_name in (
        ("runtime_spec_id", "runtime_spec_hash"),
        ("evidence_manifest_id", "evidence_manifest_hash"),
        ("source_bundle_id", "source_bundle_hash"),
    ):
        if not str(prior_manifest.get(id_name) or "").strip() or not _sha256(
            prior_manifest.get(hash_name)
        ):
            raise OwnerCorrectedStage1ReuseExportError(
                f"prior V1 manifest lacks its registered {id_name}/{hash_name} basis"
            )
    ai_summary = target_summary.get("ai_summary")
    if not isinstance(ai_summary, Mapping):
        raise OwnerCorrectedStage1ReuseExportError("corrected target summary has no ai_summary")
    summary_payload_hash = hash_json(ai_summary)
    binding = build_owner_corrected_stage1_binding(
        prior_binding,
        summary_file=summary_file,
        registry_revision=source_registry_revision,
        summary_payload_hash=summary_payload_hash,
    )
    _assert_owner_binding_preserves_prior_basis(
        prior=prior_binding,
        corrected=binding,
    )
    paper_info = target_summary.get("paper_info")
    if not isinstance(paper_info, Mapping):
        raise OwnerCorrectedStage1ReuseExportError("corrected target summary has no paper_info")
    key = _paper_key(target_summary)
    manifest = Stage1ReusableSummaryManifestV2(
        job_id=correction_job_id,
        stage_name="stage1_analyze",
        canonical_paper_key=key,
        source_paper_id=str(
            prior_manifest.get("source_paper_id")
            or paper_info.get("source_paper_id")
            or ""
        ),
        source_summary_artifact_id=summary_file.artifact_id,
        source_summary_artifact_hash=summary_file.content_hash,
        source_summary_artifact_path=summary_file.path,
        source_summary_artifact_version=summary_file.artifact_version,
        summary_payload_hash=summary_payload_hash,
        normalized_summary_payload_hash=summary_payload_hash,
        binding_hash=build_binding_hash(binding.to_dict()),
        source_pdf_content_sha256=prior_binding.source_pdf_content_sha256,
        stage1_extracted_text_hash=prior_binding.stage1_extracted_text_hash,
        stage1_semantic_input_hash=prior_binding.stage1_semantic_input_hash,
        preprocess_contract_hash=prior_binding.preprocess_contract_hash,
        prompt_id=prior_binding.prompt_id,
        prompt_version=prior_binding.prompt_version,
        prompt_sha256=prior_binding.prompt_sha256,
        prompt_template_hash=prior_binding.prompt_template_hash,
        input_builder_policy_hash=prior_binding.input_builder_policy_hash,
        summary_schema_hash=prior_binding.summary_schema_hash,
        visual_input_manifest_hash=prior_binding.visual_input_manifest_hash,
        visual_coverage_hash=prior_binding.visual_coverage_hash,
        visual_scan_schema_hash=prior_binding.visual_scan_schema_hash,
        visual_evidence_qualification=prior_binding.visual_evidence_qualification,
        provider=prior_binding.provider,
        model=prior_binding.model,
        endpoint_type=prior_binding.endpoint_type,
        provider_config_hash=prior_binding.provider_config_hash,
        summary_schema_version=str(prior_manifest.get("summary_schema_version") or ""),
        provider_receipt_closure_id="",
        provider_receipt_closure_hash="",
        provider_receipt_closure_path="",
        provider_receipt_ledger_id="",
        provider_receipt_ledger_hash="",
        provider_receipt_ledger_path="",
        source_registry_identity=f"artifact-registry:{correction_job_id}",
        source_registry_revision=source_registry_revision,
        source_kind=OWNER_APPROVED_SOURCE_CORRECTION_KIND,
        binding=binding.to_dict(),
        paper_info=dict(paper_info),
        summary_payload=dict(ai_summary),
        runtime_spec_id=str(prior_manifest.get("runtime_spec_id") or ""),
        runtime_spec_hash=str(prior_manifest.get("runtime_spec_hash") or ""),
        evidence_manifest_id=str(prior_manifest.get("evidence_manifest_id") or ""),
        evidence_manifest_hash=str(prior_manifest.get("evidence_manifest_hash") or ""),
        source_bundle_id=str(prior_manifest.get("source_bundle_id") or ""),
        source_bundle_hash=str(prior_manifest.get("source_bundle_hash") or ""),
        created_at=owner_authority.owner_action_created_at,
        producer="services.summary_correction_reuse.export_owner_corrected_stage1_reuse_authority",
        authority_kind=OWNER_APPROVED_SOURCE_CORRECTION_KIND,
        owner_correction_authority=owner_authority.to_dict(),
    )
    payload = manifest.to_dict()
    payload["manifest_content_hash"] = ""
    payload["manifest_content_hash"] = hash_json(payload)
    return payload


def _registered_owner_ref(
    registry: ArtifactRegistry,
    reference: Mapping[str, Any],
    *,
    label: str,
    artifact_type: str,
    artifact_version: str,
    status: str,
) -> ArtifactRecord:
    artifact_id = str(reference.get("artifact_id") or "")
    record = _require_record(registry.get(artifact_id), f"{label} is not registered")
    if (
        record.status != status
        or record.artifact_type != artifact_type
        or record.artifact_version != artifact_version
        or _artifact_ref(record) != dict(reference)
        or file_sha256(record.path) != record.content_hash
    ):
        raise OwnerCorrectedStage1ReuseExportError(f"{label} Registry reference is stale")
    return record


def _owner_manifest_previous_summary(
    *,
    origin_summary: Mapping[str, Any],
    prior_manifest_record: ArtifactRecord,
    prior_manifest_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], Stage1ReusableSummaryBindingV1]:
    raw_binding = _require_mapping(
        prior_manifest_payload.get("binding"),
        "origin prior V1 manifest has no binding",
    )
    binding = Stage1ReusableSummaryBindingV1.from_mapping(raw_binding)
    previous_summary = copy.deepcopy(dict(origin_summary))
    previous_summary["stage1_reuse"] = {
        "authority_kind": "typed_manifest",
        "typed_manifest_path": str(Path(prior_manifest_record.path).resolve()),
        "typed_manifest_artifact_id": prior_manifest_record.artifact_id,
        "typed_manifest_artifact_hash": prior_manifest_record.content_hash,
        "binding": dict(raw_binding),
    }
    return previous_summary, binding


def verify_owner_corrected_stage1_manifest_authority(
    *,
    manifest: Stage1ReusableSummaryManifestV2,
    manifest_path: Path,
    manifest_file_hash: str,
    manifest_artifact_id: str,
    source_summary_path: Path,
    previous_summary: Mapping[str, Any],
    binding: Stage1ReusableSummaryBindingV1,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> tuple[Stage1TypedManifestAuthorityV2 | None, str]:
    """Verify V2 receipt, derived bytes, origin basis, and Registry closure."""

    if external_registry_resolver is None:
        return None, "owner_correction_authorized_registry_resolver_missing"
    try:
        authority = Stage1OwnerCorrectionAuthorityV1.from_mapping(
            manifest.owner_correction_authority
        )
        correction_locator = _require_mapping(
            authority.correction_registry,
            "owner correction Registry locator is missing",
        )
        origin_locator = _require_mapping(
            authority.origin_registry,
            "owner origin Registry locator is missing",
        )
        correction_job_id = str(correction_locator.get("job_id") or "")
        correction_registry = _resolve_manifest_registry(
            correction_locator,
            label="owner correction",
            resolver=external_registry_resolver,
        )
        origin_registry = _resolve_manifest_registry(
            origin_locator,
            label="owner origin",
            resolver=external_registry_resolver,
        )
        if manifest.job_id != correction_job_id:
            raise OwnerCorrectedStage1ReuseExportError("V2 manifest is owned by another correction Registry")

        def resolve(job_id: str) -> ArtifactRegistry | None:
            if job_id == correction_registry.job_id:
                return correction_registry
            if job_id == origin_registry.job_id:
                return origin_registry
            return external_registry_resolver(job_id)

        correction_registry.reload()
        origin_registry.reload()
        if (
            not _sha256(origin_locator.get("registry_file_sha256"))
            or not str(origin_locator.get("registry_revision") or "").strip()
        ):
            raise OwnerCorrectedStage1ReuseExportError(
                "owner origin Registry identity or original fence is invalid"
            )

        receipt_record = _registered_owner_ref(
            correction_registry,
            authority.adoption_receipt,
            label="adoption receipt",
            artifact_type=ADOPTION_RECEIPT_ARTIFACT_TYPE,
            artifact_version=ADOPTION_RECEIPT_ARTIFACT_VERSION,
            status="ready",
        )
        derived_record = _registered_owner_ref(
            correction_registry,
            authority.derived_summary_set,
            label="derived summary set",
            artifact_type=DERIVED_SUMMARY_SET_ARTIFACT_TYPE,
            artifact_version=DERIVED_SUMMARY_SET_ARTIFACT_VERSION,
            status="ready",
        )
        origin_record = _registered_owner_ref(
            origin_registry,
            authority.origin_source,
            label="origin Stage 1 summary set",
            artifact_type="stage1_canonical_summaries",
            artifact_version="v1",
            status="ready",
        )
        prior_manifest_record = _registered_owner_ref(
            origin_registry,
            authority.origin_prior_manifest,
            label="origin prior reusable manifest",
            artifact_type="stage1_reusable_summary_manifest",
            artifact_version="v1",
            status="ready",
        )
        candidate_record = _registered_owner_ref(
            correction_registry,
            authority.candidate,
            label="quarantined correction candidate",
            artifact_type="stage1_summary_correction_candidate",
            artifact_version="v1",
            status="quarantined",
        )
        proposal_record = _registered_owner_ref(
            correction_registry,
            authority.proposal,
            label="quarantined correction proposal",
            artifact_type="stage1_summary_correction_proposal",
            artifact_version="v1",
            status="quarantined",
        )
        source_file_record = _require_record(
            correction_registry.get(manifest.source_summary_artifact_id),
            "owner-corrected summary_file is not registered",
        )
        if (
            source_file_record.status != "ready"
            or source_file_record.artifact_type != "summary_file"
            or source_file_record.artifact_version != "v1"
            or source_file_record.content_hash != manifest.source_summary_artifact_hash
            or str(Path(source_file_record.path).resolve())
            != str(source_summary_path.resolve())
        ):
            raise OwnerCorrectedStage1ReuseExportError("owner-corrected summary_file authority is stale")

        manifest_records = [
            record
            for record in correction_registry.list_records()
            if record.status == "ready"
            and record.artifact_type == "stage1_reusable_summary_manifest"
            and record.artifact_version == "v2"
            and record.content_hash == manifest_file_hash
            and file_sha256(record.path) == manifest_file_hash
        ]
        if len(manifest_records) != 1:
            raise OwnerCorrectedStage1ReuseExportError(
                "owner-corrected V2 manifest Registry record is missing or ambiguous"
            )
        manifest_record = manifest_records[0]
        expected_manifest_dependencies = [
            ArtifactDependencyRefV2.from_record(source_file_record),
            ArtifactDependencyRefV2.from_record(receipt_record),
            ArtifactDependencyRefV2.from_record(derived_record),
            ArtifactDependencyRefV2.from_record(origin_record, dependency_kind="external_job"),
            ArtifactDependencyRefV2.from_record(prior_manifest_record, dependency_kind="external_job"),
        ]
        if [item.to_dict() for item in manifest_record.depends_on] != [
            item.to_dict() for item in expected_manifest_dependencies
        ]:
            raise OwnerCorrectedStage1ReuseExportError(
                "owner-corrected V2 manifest dependency closure differs from its authority refs"
            )
        correction_registry.verify_ready_artifact_closure(
            manifest_record,
            external_registry_resolver=resolve,
        )
        origin_registry.verify_ready_artifact_closure(
            prior_manifest_record,
            external_registry_resolver=resolve,
        )

        try:
            adoption_verified = verify_source_summary_correction_adoption(
                source_registry=origin_registry,
                destination_registry=correction_registry,
                adoption_receipt_artifact_id=receipt_record.artifact_id,
                external_registry_resolver=resolve,
            )
        except (SourceSummaryCorrectionAdoptionError, RegistryError, OSError, ValueError, TypeError) as exc:
            raise OwnerCorrectedStage1ReuseExportError(
                f"owner correction receipt/candidate no longer verifies: {exc}"
            ) from exc
        if not adoption_verified.verified:
            raise OwnerCorrectedStage1ReuseExportError("owner correction receipt/candidate verification failed")

        receipt_payload = _read_json_object(receipt_record.path, label="owner correction receipt")
        derived_payload = _read_json_object(derived_record.path, label="owner derived summary set")
        origin_payload = _read_json_object(origin_record.path, label="origin Stage 1 summary set")
        prior_payload = _read_json_object(prior_manifest_record.path, label="origin prior V1 manifest")
        candidate_payload = _read_json_object(candidate_record.path, label="correction candidate")
        proposal_payload = _read_json_object(proposal_record.path, label="correction proposal")
        source_rows = _read_json_rows(source_summary_path, label="owner-corrected summary_file")
        derived_rows = _require_rows(derived_payload.get("summaries"), "derived set summaries are missing")
        candidate_rows = _require_rows(candidate_payload.get("candidate_summaries"), "candidate summaries are missing")
        origin_rows = _require_rows(origin_payload.get("summaries"), "origin summaries are missing")
        projected_derived_rows = _summary_file_projection(derived_rows)
        target_key = authority.target_canonical_paper_key
        origin_by_key = _summary_map(origin_rows, label="origin summary set")
        source_by_key = _summary_map(source_rows, label="owner summary_file")
        derived_by_key = _summary_map(derived_rows, label="derived summary set")
        if (
            source_rows != projected_derived_rows
            or derived_rows != candidate_rows
            or list(source_by_key) != list(origin_by_key)
            or target_key not in origin_by_key
            or target_key not in source_by_key
            or list(source_by_key) != list(derived_by_key)
        ):
            raise OwnerCorrectedStage1ReuseExportError("owner summary_file/derived/candidate identity sets differ")
        if hash_json(derived_rows) != authority.candidate_summary_set_hash:
            raise OwnerCorrectedStage1ReuseExportError("owner candidate summary-set hash is invalid")
        if hash_json(origin_rows) != authority.source_summary_set_hash:
            raise OwnerCorrectedStage1ReuseExportError("owner origin summary-set hash is invalid")
        origin_target = origin_by_key[target_key]
        corrected_target = derived_by_key[target_key]
        if (
            hash_json(origin_target) != authority.target_before_summary_hash
            or hash_json(corrected_target) != authority.target_after_summary_hash
            or hash_json(origin_target.get("ai_summary")) != authority.prior_summary_payload_hash
            or hash_json(corrected_target.get("ai_summary")) != authority.corrected_summary_payload_hash
            or dict(corrected_target.get("ai_summary") or {}) != dict(manifest.summary_payload)
        ):
            raise OwnerCorrectedStage1ReuseExportError("owner corrected target payload hashes do not match")
        if hash_json(derived_payload.get("summaries")) != str(
            derived_payload.get("candidate_summary_set_hash") or ""
        ):
            raise OwnerCorrectedStage1ReuseExportError("derived owner summary set hash is invalid")
        if (
            candidate_record.content_hash != str(authority.candidate.get("content_hash") or "")
            or str(candidate_payload.get("candidate_summary_set_hash") or "")
            != authority.candidate_summary_set_hash
            or hash_json(proposal_payload.get("normalized_proposal"))
            != str(receipt_payload.get("normalized_proposal_hash") or "")
            or proposal_payload.get("normalized_proposal") != receipt_payload.get("normalized_proposal")
        ):
            raise OwnerCorrectedStage1ReuseExportError("candidate/proposal bytes do not match the adoption receipt")
        owner_action = _require_mapping(receipt_payload.get("owner_action"), "receipt owner action is missing")
        if (
            dict(owner_action) != dict(authority.owner_action)
            or str(receipt_payload.get("created_at") or "") != authority.owner_action_created_at
            or str(receipt_payload.get("action_id") or "") != authority.action_id
        ):
            raise OwnerCorrectedStage1ReuseExportError("owner action fields do not match the adoption receipt")

        origin_target_prior, prior_binding = _owner_manifest_previous_summary(
            origin_summary=origin_target,
            prior_manifest_record=prior_manifest_record,
            prior_manifest_payload=prior_payload,
        )
        prior_authority, prior_reason = verify_stage1_typed_manifest_authority(
            origin_target_prior,
            prior_binding,
            external_registry_resolver=resolve,
        )
        if prior_authority is None or prior_authority.manifest.artifact_version != "v1":
            raise OwnerCorrectedStage1ReuseExportError(
                f"origin prior V1 manifest no longer verifies: {prior_reason}"
            )
        source_revision = str(
            source_file_record.metadata.get("source_authority_registry_revision") or ""
        )
        expected_binding = build_owner_corrected_stage1_binding(
            prior_binding,
            summary_file=source_file_record,
            registry_revision=source_revision,
            summary_payload_hash=authority.corrected_summary_payload_hash,
        )
        if build_binding_hash(expected_binding.to_dict()) != manifest.binding_hash:
            raise OwnerCorrectedStage1ReuseExportError(
                "owner-corrected binding does not preserve the verified prior V1 basis"
            )
        if (
            manifest.authority_kind != OWNER_APPROVED_SOURCE_CORRECTION_KIND
            or manifest.source_kind != OWNER_APPROVED_SOURCE_CORRECTION_KIND
            or binding.source_kind != OWNER_APPROVED_SOURCE_CORRECTION_KIND
            or manifest.provider_receipt_closure_id
            or manifest.provider_receipt_closure_hash
            or manifest.provider_receipt_closure_path
            or manifest.provider_receipt_ledger_id
            or manifest.provider_receipt_ledger_hash
            or manifest.provider_receipt_ledger_path
        ):
            raise OwnerCorrectedStage1ReuseExportError(
                "owner-corrected manifest has an invalid authority kind or provider receipt fields"
            )
        return (
            Stage1TypedManifestAuthorityV2(
                manifest=manifest,
                manifest_path=str(manifest_path),
                manifest_file_hash=manifest_file_hash,
                manifest_artifact_id=manifest_artifact_id,
                source_summary_path=str(source_summary_path),
                provider_closure_path="",
                provider_ledger_path="",
                owner_correction_receipt_path=receipt_record.path,
                owner_correction_derived_summary_path=derived_record.path,
                owner_correction_origin_source_path=origin_record.path,
                owner_correction_prior_manifest_path=prior_manifest_record.path,
            ),
            "owner_corrected_typed_manifest_authority_verified",
        )
    except (OSError, RegistryError, ValueError, TypeError, KeyError) as exc:
        return None, f"owner_corrected_typed_manifest_untrusted:{exc}"



def export_owner_corrected_stage1_reuse_authority(
    *,
    origin_registry: ArtifactRegistry,
    correction_registry: ArtifactRegistry,
    correction_workspace: JobWorkspace,
    adoption_receipt_artifact_id: str,
    publication_context: Any,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> OwnerCorrectedStage1ReuseExportResultV1:
    """Export a verified owner receipt as one V2 manifest plus prior V1 refs.

    The corrected target gets a new ``stage1_reusable_summary_manifest/v2``.
    Every unchanged paper keeps one uniquely verified existing V1 manifest.
    Missing or ambiguous prior basis for any row blocks before this function
    publishes the summary file or any manifests.
    """

    _workspace_registry_checks(
        origin_registry=origin_registry,
        correction_registry=correction_registry,
        workspace=correction_workspace,
    )
    resolver = _resolve_registries(
        origin_registry,
        correction_registry,
        external_registry_resolver,
    )
    origin_registry.reload()
    correction_registry.reload()
    origin_registry_path = Path(origin_registry.registry_path)
    origin_registry_hash_before = file_sha256(origin_registry_path)
    origin_registry_revision_before = origin_registry.revision
    origin_pointer_before = origin_registry.get("stage1_summaries")
    correction_pointer_before = correction_registry.get("stage1_summaries")

    try:
        adoption_verification = verify_source_summary_correction_adoption(
            source_registry=origin_registry,
            destination_registry=correction_registry,
            adoption_receipt_artifact_id=adoption_receipt_artifact_id,
            external_registry_resolver=resolver,
        )
    except (SourceSummaryCorrectionAdoptionError, RegistryError, OSError, ValueError, TypeError) as exc:
        raise OwnerCorrectedStage1ReuseExportError(
            f"owner correction adoption receipt is not verified: {exc}"
        ) from exc
    if not adoption_verification.verified:
        raise OwnerCorrectedStage1ReuseExportError("owner correction adoption receipt failed verification")

    receipt_record = _require_record(
        correction_registry.get(adoption_receipt_artifact_id),
        "approved correction receipt is not registered",
    )
    receipt_payload = _read_json_object(receipt_record.path, label="approved correction receipt")
    if (
        receipt_record.status != "ready"
        or receipt_record.artifact_type != ADOPTION_RECEIPT_ARTIFACT_TYPE
        or receipt_record.artifact_version != ADOPTION_RECEIPT_ARTIFACT_VERSION
        or receipt_payload.get("usable_as_stage1_reuse") is not False
        or receipt_payload.get("canonical_pointer_advanced") is not False
    ):
        raise OwnerCorrectedStage1ReuseExportError(
            "approved correction receipt has an unsupported authority state"
        )
    owner_action = _require_mapping(
        receipt_payload.get("owner_action"),
        "approved correction receipt has no manual owner action",
    )
    if (
        owner_action.get("method") != "manual_operator_action"
        or not str(owner_action.get("actor") or "").strip()
        or not str(owner_action.get("reason") or "").strip()
    ):
        raise OwnerCorrectedStage1ReuseExportError(
            "approved correction receipt has incomplete actor/reason evidence"
        )

    source_ref = _require_mapping(
        receipt_payload.get("source_summary"),
        "approved correction receipt has no origin summary ref",
    )
    source_record = _require_record(
        origin_registry.get(str(source_ref.get("artifact_id") or "")),
        "approved correction origin summary is not registered in the origin Registry",
    )
    if (
        source_record.status != "ready"
        or source_record.artifact_type != "stage1_canonical_summaries"
        or source_record.artifact_version != "v1"
        or _artifact_ref(source_record) != dict(source_ref)
    ):
        raise OwnerCorrectedStage1ReuseExportError(
            "approved correction origin summary ref is stale or has the wrong type"
        )
    origin_registry.verify_ready_artifact_closure(
        source_record,
        external_registry_resolver=resolver,
    )
    origin_doc = _read_json_object(source_record.path, label="origin Stage 1 summary set")
    origin_rows = _require_rows(origin_doc.get("summaries"), "origin Stage 1 summaries are missing")
    origin_set_hash = str(origin_doc.get("summary_set_hash") or "")
    if not _sha256(origin_set_hash) or hash_json(origin_rows) != origin_set_hash:
        raise OwnerCorrectedStage1ReuseExportError("origin Stage 1 summary-set hash is invalid")
    if str(receipt_payload.get("source_summary_set_hash") or "") != origin_set_hash:
        raise OwnerCorrectedStage1ReuseExportError("receipt source summary-set hash is stale")

    derived_ref = _require_mapping(
        receipt_payload.get("derived_summary"),
        "approved correction receipt has no derived summary ref",
    )
    derived_record = _require_record(
        correction_registry.get(str(derived_ref.get("artifact_id") or "")),
        "approved correction derived summary is not registered",
    )
    if (
        derived_record.status != "ready"
        or derived_record.artifact_type != DERIVED_SUMMARY_SET_ARTIFACT_TYPE
        or derived_record.artifact_version != DERIVED_SUMMARY_SET_ARTIFACT_VERSION
        or _artifact_ref(derived_record)
        != {key: value for key, value in dict(derived_ref).items() if key != "summary_count"}
    ):
        raise OwnerCorrectedStage1ReuseExportError(
            "approved correction derived summary ref is stale or has the wrong type"
        )
    derived_payload = _read_json_object(derived_record.path, label="approved derived summary set")
    corrected_rows = _require_rows(derived_payload.get("summaries"), "derived summary rows are missing")
    source_by_key = _summary_map(origin_rows, label="origin Stage 1 summaries")
    corrected_by_key = _summary_map(corrected_rows, label="owner-corrected Stage 1 summaries")
    source_keys = [_paper_key(row) for row in origin_rows]
    corrected_keys = [_paper_key(row) for row in corrected_rows]
    if source_keys != corrected_keys:
        raise OwnerCorrectedStage1ReuseExportError(
            "owner correction changed the Stage 1 paper identity set or order"
        )
    if hash_json(corrected_rows) != str(receipt_payload.get("candidate_summary_set_hash") or ""):
        raise OwnerCorrectedStage1ReuseExportError("approved correction candidate-set hash is invalid")
    proposal = _require_mapping(
        receipt_payload.get("normalized_proposal"),
        "approved correction receipt has no normalized proposal",
    )
    target_key = str(proposal.get("target_canonical_paper_key") or "").strip()
    if target_key not in source_by_key or target_key not in corrected_by_key:
        raise OwnerCorrectedStage1ReuseExportError("approved correction target is not in the source set")
    changed_keys = [
        key
        for key in source_keys
        if hash_json(source_by_key[key].get("ai_summary"))
        != hash_json(corrected_by_key[key].get("ai_summary"))
    ]
    if changed_keys != [target_key]:
        raise OwnerCorrectedStage1ReuseExportError(
            "approved correction must change exactly its declared target summary"
        )
    for key in source_keys:
        if key == target_key:
            continue
        if source_by_key[key] != corrected_by_key[key]:
            raise OwnerCorrectedStage1ReuseExportError(
                f"owner correction changed an unchanged paper row: {key}"
            )
    target_before_hash = hash_json(source_by_key[target_key])
    target_after_hash = hash_json(corrected_by_key[target_key])
    candidate_ref = _require_mapping(
        receipt_payload.get("candidate"),
        "approved correction receipt has no candidate ref",
    )
    candidate_record = _require_record(
        correction_registry.get(str(candidate_ref.get("artifact_id") or "")),
        "quarantined correction candidate is missing",
    )
    candidate_payload = _read_json_object(candidate_record.path, label="approved correction candidate")
    if (
        target_before_hash != str(proposal.get("before_summary_hash") or "")
        or target_before_hash != str(candidate_payload.get("target_before_summary_hash") or "")
        or target_after_hash != str(candidate_payload.get("target_after_summary_hash") or "")
    ):
        raise OwnerCorrectedStage1ReuseExportError("approved correction target before/after hashes are invalid")

    # Resolve every prior authority before publishing anything.  The unchanged
    # rows retain these existing manifests; only the approved target gets V2.
    prior_rows: dict[str, tuple[ArtifactRecord, dict[str, Any], Stage1ReusableSummaryBindingV1]] = {}
    for key in source_keys:
        prior_rows[key] = _verify_prior_manifest_record(
            origin_registry=origin_registry,
            origin_source_record=source_record,
            source_summary=source_by_key[key],
            external_registry_resolver=resolver,
        )

    receipt_snapshot_ref = _require_mapping(
        receipt_payload.get("source_snapshot"),
        "approved correction receipt has no source snapshot ref",
    )
    proposal_ref = _require_mapping(
        receipt_payload.get("proposal"),
        "approved correction receipt has no proposal ref",
    )
    source_snapshot_record = _require_record(
        correction_registry.get(str(receipt_snapshot_ref.get("artifact_id") or "")),
        "quarantined source snapshot is missing",
    )
    proposal_record = _require_record(
        correction_registry.get(str(proposal_ref.get("artifact_id") or "")),
        "quarantined correction proposal is missing",
    )
    for record, expected_ref, label in (
        (source_snapshot_record, receipt_snapshot_ref, "source snapshot"),
        (candidate_record, candidate_ref, "candidate"),
        (proposal_record, proposal_ref, "proposal"),
    ):
        if record.status != "quarantined" or _artifact_ref(record) != dict(expected_ref):
            raise OwnerCorrectedStage1ReuseExportError(
                f"approved correction {label} ref is stale or no longer quarantined"
            )
    origin_registry_hash_before = file_sha256(origin_registry.registry_path)
    origin_registry_revision_before = origin_registry.revision
    correction_pointer_before = correction_registry.get("stage1_summaries")

    projected_corrected_rows = _summary_file_projection(corrected_rows)
    summary_set_hash = hash_json(corrected_rows)
    summary_file_bytes = _json_bytes(projected_corrected_rows)
    summary_file_hash = hashlib.sha256(summary_file_bytes).hexdigest()
    summary_file_id = f"stage1:owner_corrected_summary_file:{summary_file_hash[:32]}"
    summary_file_dependencies = [
        ArtifactDependencyRefV2.from_record(receipt_record),
        ArtifactDependencyRefV2.from_record(derived_record),
        ArtifactDependencyRefV2.from_record(source_record, dependency_kind="external_job"),
    ]
    source_file_record = _publish_or_verify(
        publication_context=publication_context,
        registry=correction_registry,
        workspace=correction_workspace,
        path=correction_workspace.artifact_path(
            f"stage1_summary_correction/reuse/{summary_file_hash[:24]}/summaries.json"
        ),
        payload=projected_corrected_rows,
        artifact_id=summary_file_id,
        artifact_role="stage1_owner_corrected_summary_source",
        artifact_type="summary_file",
        artifact_version="v1",
        producer="services.summary_correction_reuse.export_owner_corrected_stage1_reuse_authority",
        dependencies=summary_file_dependencies,
        external_registry_resolver=resolver,
        metadata={
            "owner_action_id": str(receipt_payload.get("action_id") or ""),
            "summary_set_hash": summary_set_hash,
            "usable_as_stage1_reuse": False,
        },
    )
    source_file_record, source_registry_revision = _ensure_source_file_revision(
        correction_registry,
        source_file_record,
        external_registry_resolver=resolver,
    )

    prior_target_record, prior_target_payload, prior_target_binding = prior_rows[target_key]
    action_owner = dict(owner_action)
    authority = Stage1OwnerCorrectionAuthorityV1(
        action_id=str(receipt_payload.get("action_id") or ""),
        owner_action=action_owner,
        owner_action_created_at=str(receipt_payload.get("created_at") or ""),
        adoption_receipt=_artifact_ref(receipt_record),
        derived_summary_set=_artifact_ref(derived_record),
        correction_registry={
            "job_id": correction_registry.job_id,
            "registry_path": str(Path(correction_registry.registry_path).resolve()),
        },
        origin_registry={
            "job_id": origin_registry.job_id,
            "registry_path": str(Path(origin_registry.registry_path).resolve()),
            "registry_file_sha256": origin_registry_hash_before,
            "registry_revision": str(origin_registry_revision_before),
        },
        origin_source=_artifact_ref(source_record),
        origin_prior_manifest=_artifact_ref(prior_target_record),
        candidate=_artifact_ref(candidate_record),
        proposal=_artifact_ref(proposal_record),
        target_canonical_paper_key=target_key,
        source_summary_set_hash=origin_set_hash,
        candidate_summary_set_hash=hash_json(corrected_rows),
        prior_summary_payload_hash=hash_json(source_by_key[target_key]["ai_summary"]),
        corrected_summary_payload_hash=hash_json(corrected_by_key[target_key]["ai_summary"]),
        target_before_summary_hash=target_before_hash,
        target_after_summary_hash=target_after_hash,
    )
    v2_payload = _owner_corrected_manifest_payload(
        prior_manifest=prior_target_payload,
        prior_binding=prior_target_binding,
        summary_file=source_file_record,
        source_registry_revision=source_registry_revision,
        target_summary=corrected_by_key[target_key],
        owner_authority=authority,
        correction_job_id=correction_registry.job_id,
    )
    manifest_hash = hashlib.sha256(_json_bytes(v2_payload)).hexdigest()
    manifest_id = f"stage1:owner_corrected_summary_manifest:{manifest_hash[:32]}"
    manifest_dependencies: list[ArtifactDependencyRefV2] = [
        ArtifactDependencyRefV2.from_record(source_file_record),
        ArtifactDependencyRefV2.from_record(receipt_record),
        ArtifactDependencyRefV2.from_record(derived_record),
        ArtifactDependencyRefV2.from_record(source_record, dependency_kind="external_job"),
        ArtifactDependencyRefV2.from_record(prior_target_record, dependency_kind="external_job"),
    ]
    manifest_path = correction_workspace.artifact_path(
        f"stage1_summary_correction/reuse/{manifest_hash[:24]}/target_manifest.json"
    )
    v2_record = _publish_or_verify(
        publication_context=publication_context,
        registry=correction_registry,
        workspace=correction_workspace,
        path=manifest_path,
        payload=v2_payload,
        artifact_id=manifest_id,
        artifact_role="stage1_summary_manifest",
        artifact_type="stage1_reusable_summary_manifest",
        artifact_version="v2",
        producer="services.summary_correction_reuse.export_owner_corrected_stage1_reuse_authority",
        dependencies=manifest_dependencies,
        external_registry_resolver=resolver,
        metadata={
            "authority": True,
            "authority_kind": OWNER_APPROVED_SOURCE_CORRECTION_KIND,
            "owner_action_id": authority.action_id,
            "source_summary_artifact_id": source_file_record.artifact_id,
            "source_summary_artifact_hash": source_file_record.content_hash,
        },
    )
    target_manifest_ref = _artifact_ref(v2_record)

    manifest_refs: list[dict[str, Any]] = []
    unchanged_manifest_records: list[ArtifactRecord] = []
    for key in source_keys:
        if key == target_key:
            ref = {
                **target_manifest_ref,
                "canonical_paper_key": key,
                "authority_version": "owner_corrected_v2",
                "summary_payload_hash": authority.corrected_summary_payload_hash,
            }
        else:
            prior_record, prior_payload, _prior_binding = prior_rows[key]
            unchanged_manifest_records.append(prior_record)
            ref = {
                **_artifact_ref(prior_record),
                "canonical_paper_key": key,
                "authority_version": "typed_manifest_v1",
                "summary_payload_hash": str(
                    prior_payload.get("summary_payload_hash")
                    or prior_payload.get("normalized_summary_payload_hash")
                    or ""
                ),
            }
        manifest_refs.append(ref)

    bundle_payload = {
        "artifact_type": OWNER_CORRECTED_IMPORT_BUNDLE_TYPE,
        "artifact_version": OWNER_CORRECTED_IMPORT_BUNDLE_VERSION,
        "job_id": correction_registry.job_id,
        "status": "ready_for_stage1_import",
        "action_id": authority.action_id,
        "target_canonical_paper_key": target_key,
        "paper_count": len(manifest_refs),
        "source_summary_set_hash": origin_set_hash,
        "candidate_summary_set_hash": summary_set_hash,
        "adoption_receipt": _artifact_ref(receipt_record),
        "derived_summary_set": _artifact_ref(derived_record),
        "origin_source": _artifact_ref(source_record),
        "summary_file": _artifact_ref(source_file_record),
        "owner_corrected_manifest": target_manifest_ref,
        "manifest_refs": manifest_refs,
        "usable_as_stage1_reuse": False,
        "canonical_pointer_advanced": False,
        "provider_calls": 0,
        "provider_receipt_ids_created": [],
    }
    bundle_hash = hashlib.sha256(_json_bytes(bundle_payload)).hexdigest()
    bundle_id = f"stage1:owner_corrected_import_bundle:{bundle_hash[:32]}"
    bundle_dependencies = [
        ArtifactDependencyRefV2.from_record(source_file_record),
        ArtifactDependencyRefV2.from_record(receipt_record),
        ArtifactDependencyRefV2.from_record(derived_record),
        ArtifactDependencyRefV2.from_record(v2_record),
        ArtifactDependencyRefV2.from_record(source_record, dependency_kind="external_job"),
        *(
            ArtifactDependencyRefV2.from_record(record, dependency_kind="external_job")
            for record in unchanged_manifest_records
        ),
    ]
    bundle_path = correction_workspace.artifact_path(
        f"stage1_summary_correction/reuse/{bundle_hash[:24]}/owner_import_bundle.json"
    )
    bundle_record = _publish_or_verify(
        publication_context=publication_context,
        registry=correction_registry,
        workspace=correction_workspace,
        path=bundle_path,
        payload=bundle_payload,
        artifact_id=bundle_id,
        artifact_role="stage1_owner_corrected_reuse_import_bundle",
        artifact_type=OWNER_CORRECTED_IMPORT_BUNDLE_TYPE,
        artifact_version=OWNER_CORRECTED_IMPORT_BUNDLE_VERSION,
        producer="services.summary_correction_reuse.export_owner_corrected_stage1_reuse_authority",
        dependencies=bundle_dependencies,
        external_registry_resolver=resolver,
        metadata={
            "owner_action_id": authority.action_id,
            "target_canonical_paper_key": target_key,
            "manifest_count": len(manifest_refs),
        },
    )

    imported_target = {
        "status": "success",
        "paper_info": dict(corrected_by_key[target_key]["paper_info"]),
        "source_mode": str(corrected_by_key[target_key].get("source_mode") or ""),
        "ai_summary": dict(corrected_by_key[target_key]["ai_summary"]),
        "provider": {"transport_count": 0, "receipt_ids": []},
        "stage1_reuse": {
            "authority_kind": "typed_manifest",
            "typed_manifest_path": v2_record.path,
            "typed_manifest_artifact_id": v2_record.artifact_id,
            "typed_manifest_artifact_hash": v2_record.content_hash,
            "binding": dict(v2_payload["binding"]),
        },
    }
    target_binding = Stage1ReusableSummaryBindingV1.from_mapping(v2_payload["binding"])
    typed_authority, verify_reason = verify_stage1_typed_manifest_authority(
        imported_target,
        target_binding,
        external_registry_resolver=resolver,
    )
    if not isinstance(typed_authority, Stage1TypedManifestAuthorityV2):
        raise OwnerCorrectedStage1ReuseExportError(
            f"published owner-corrected V2 manifest did not reverify: {verify_reason}"
        )

    correction_registry.reload()
    source_file_record = _require_record(
        correction_registry.get(source_file_record.artifact_id),
        "owner summary_file disappeared after publication",
    )
    v2_record = _require_record(
        correction_registry.get(v2_record.artifact_id),
        "owner-corrected V2 manifest disappeared after publication",
    )
    bundle_record = _require_record(
        correction_registry.get(bundle_record.artifact_id),
        "owner import bundle disappeared after publication",
    )
    origin_registry.reload()
    origin_unchanged = bool(
        file_sha256(origin_registry_path) == origin_registry_hash_before
        and origin_registry.revision == origin_registry_revision_before
    )
    if not origin_unchanged:
        raise OwnerCorrectedStage1ReuseExportError(
            "origin Registry changed during owner-corrected manifest export"
        )
    if origin_registry.get("stage1_summaries") != origin_pointer_before:
        raise OwnerCorrectedStage1ReuseExportError(
            "origin Stage 1 current pointer changed during owner-corrected manifest export"
        )
    if correction_registry.get("stage1_summaries") != correction_pointer_before:
        raise OwnerCorrectedStage1ReuseExportError(
            "correction Stage 1 current pointer changed during owner-corrected manifest export"
        )
    return OwnerCorrectedStage1ReuseExportResultV1(
        status="exported",
        action_id=authority.action_id,
        target_canonical_paper_key=target_key,
        summary_file_artifact_id=source_file_record.artifact_id,
        summary_file_artifact_hash=source_file_record.content_hash,
        summary_file_path=source_file_record.path,
        owner_corrected_manifest_artifact_id=v2_record.artifact_id,
        owner_corrected_manifest_artifact_hash=v2_record.content_hash,
        owner_corrected_manifest_path=v2_record.path,
        owner_import_bundle_artifact_id=bundle_record.artifact_id,
        owner_import_bundle_artifact_hash=bundle_record.content_hash,
        owner_import_bundle_path=bundle_record.path,
        paper_count=len(manifest_refs),
        unchanged_prior_manifest_count=len(unchanged_manifest_records),
        manifest_refs=tuple(manifest_refs),
        origin_registry_unchanged=origin_unchanged,
    )
