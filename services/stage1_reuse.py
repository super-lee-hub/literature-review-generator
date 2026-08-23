"""Typed eligibility and provenance for Stage 1 summary reuse."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from services.artifact_registry import ArtifactRegistry, file_sha256
from services.prompt_registry import PromptRegistry
from services.stage1_visual_scan import VISUAL_OBSERVATIONS_VERSION, VISUAL_SCAN_PROMPT_ID
from runtime.provider_runtime import hash_json


STAGE1_REUSE_BINDING_VERSION = "v1"
STAGE1_REUSE_POLICY = "exact_summary_reuse_v1"

_LEGACY_COMPARISON_FIELDS = (
    "canonical_paper_key",
    "source_paper_id",
    "source_mode",
    "source_pdf_hash",
    "source_pdf_fingerprint",
    "preprocess_hash",
    "stage1_input_hash",
    "prompt_hash",
    "builder_version",
    "provider",
    "model",
    "endpoint_type",
    "provider_config_hash",
    "schema_hash",
    "visual_provenance_hash",
)

# The legacy source_pdf_hash was used for a semantic/preprocess digest by
# earlier writers.  Keep reading it, but never use it as a PDF byte identity.
_STRUCTURED_COMPARISON_FIELDS = (
    "canonical_paper_key",
    "source_mode",
    "source_pdf_content_sha256",
    "stage1_extracted_text_hash",
    "stage1_semantic_input_hash",
    "preprocess_contract_hash",
    "prompt_id",
    "prompt_version",
    "prompt_sha256",
    "prompt_template_hash",
    "input_builder_policy_hash",
    "provider",
    "model",
    "endpoint_type",
    "provider_config_hash",
    "summary_schema_hash",
    "visual_input_manifest_hash",
    "visual_coverage_hash",
    "visual_scan_schema_hash",
    # Provenance facts are projected here as well.  They are checked against
    # the prior authority below; an empty current-run value is expected before
    # reuse succeeds and is therefore not treated as a current-input miss.
    "normalized_summary_payload_hash",
    "summary_payload_hash",
    "source_authority_job_id",
    "source_authority_registry_id",
    "source_authority_registry_revision",
    "source_authority_artifact_id",
    "source_authority_artifact_hash",
    "source_summary_manifest_id",
    "source_summary_manifest_hash",
    "source_provider_receipt_closure_id",
    "source_provider_receipt_closure_hash",
    "source_provider_receipt_ledger_id",
    "source_provider_receipt_ledger_hash",
)

_PROVENANCE_COMPARISON_FIELDS = frozenset(
    {
        "normalized_summary_payload_hash",
        "summary_payload_hash",
        "source_authority_job_id",
        "source_authority_registry_id",
        "source_authority_registry_revision",
        "source_authority_artifact_id",
        "source_authority_artifact_hash",
        "source_summary_manifest_id",
        "source_summary_manifest_hash",
        "source_provider_receipt_closure_id",
        "source_provider_receipt_closure_hash",
        "source_provider_receipt_ledger_id",
        "source_provider_receipt_ledger_hash",
    }
)

# Provider names are optional in the application configuration.  An omitted
# provider is still a stable binding when both the prior and current runs omit
# it; a value appearing or changing remains a binding change.
_OPTIONAL_COMPARISON_FIELDS = frozenset({"provider", "model", "endpoint_type"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _hash_file(path: str) -> str:
    target = Path(path).expanduser()
    if not target.is_file():
        return ""
    try:
        return file_sha256(str(target))
    except (OSError, TypeError, ValueError):
        return ""


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def _as_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


@dataclass(frozen=True)
class Stage1VisualEvidenceQualificationV1:
    """Achieved visual evidence facts used to gate exact summary reuse.

    Input identity remains in :class:`Stage1ReusableSummaryBindingV1`; this
    section records what the run actually proved.  Keeping the two separate
    prevents a partial scan from becoming a new input identity while still
    making the partial result impossible to use as an exact-reuse authority.
    """

    artifact_type: str = "stage1_visual_evidence_qualification"
    artifact_version: str = "v1"
    coverage_artifact_id: str = ""
    coverage_artifact_hash: str = ""
    coverage_artifact_path: str = ""
    observation_artifact_ids: tuple[str, ...] = ()
    observation_artifact_hashes: tuple[str, ...] = ()
    observation_artifact_paths: tuple[str, ...] = ()
    required_nonblank_page_count: int = 0
    required_page_ids: tuple[str, ...] = ()
    sent_page_ids: tuple[str, ...] = ()
    observed_page_ids: tuple[str, ...] = ()
    render_failed_page_ids: tuple[str, ...] = ()
    scan_failed_page_ids: tuple[str, ...] = ()
    transport_omissions: tuple[Mapping[str, Any], ...] = ()
    scan_coverage_status: str = "not_required"
    final_synthesis_modality: str = "text_only"
    final_raw_visual_recheck_status: str = "not_required"
    evidence_coverage_status: str = "complete"
    require_complete_visual_coverage: bool = True
    visual_observation_artifact_version: str = ""
    visual_scan_prompt_id: str = ""
    visual_scan_prompt_version: str = ""
    visual_scan_prompt_sha256: str = ""
    visual_scan_schema_hash: str = ""

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "Stage1VisualEvidenceQualificationV1":
        raw = dict(value or {})
        omissions = raw.get("transport_omissions")
        normalized_omissions: tuple[Mapping[str, Any], ...] = tuple(
            dict(item) for item in omissions if isinstance(item, Mapping)
        ) if isinstance(omissions, (list, tuple)) else ()
        try:
            required_count = max(0, int(raw.get("required_nonblank_page_count") or 0))
        except (TypeError, ValueError):
            required_count = 0
        return cls(
            artifact_type=_text(raw.get("artifact_type")) or cls.artifact_type,
            artifact_version=_text(raw.get("artifact_version")) or cls.artifact_version,
            coverage_artifact_id=_text(raw.get("coverage_artifact_id")),
            coverage_artifact_hash=_text(raw.get("coverage_artifact_hash")),
            coverage_artifact_path=_text(raw.get("coverage_artifact_path")),
            observation_artifact_ids=cls._strings(raw.get("observation_artifact_ids")),
            observation_artifact_hashes=cls._strings(raw.get("observation_artifact_hashes")),
            observation_artifact_paths=cls._strings(raw.get("observation_artifact_paths")),
            required_nonblank_page_count=required_count,
            required_page_ids=cls._strings(raw.get("required_page_ids")),
            sent_page_ids=cls._strings(raw.get("sent_page_ids")),
            observed_page_ids=cls._strings(raw.get("observed_page_ids")),
            render_failed_page_ids=cls._strings(raw.get("render_failed_page_ids")),
            scan_failed_page_ids=cls._strings(raw.get("scan_failed_page_ids")),
            transport_omissions=normalized_omissions,
            scan_coverage_status=_text(raw.get("scan_coverage_status")) or "not_required",
            final_synthesis_modality=_text(raw.get("final_synthesis_modality")) or "text_only",
            final_raw_visual_recheck_status=(
                _text(raw.get("final_raw_visual_recheck_status")) or "not_required"
            ),
            evidence_coverage_status=_text(raw.get("evidence_coverage_status")) or "complete",
            require_complete_visual_coverage=_as_bool(
                raw.get("require_complete_visual_coverage"), True
            ),
            visual_observation_artifact_version=_text(
                raw.get("visual_observation_artifact_version")
            ),
            visual_scan_prompt_id=_text(raw.get("visual_scan_prompt_id")),
            visual_scan_prompt_version=_text(raw.get("visual_scan_prompt_version")),
            visual_scan_prompt_sha256=_text(raw.get("visual_scan_prompt_sha256")),
            visual_scan_schema_hash=_text(raw.get("visual_scan_schema_hash")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for field_name in (
            "observation_artifact_ids", "observation_artifact_hashes",
            "observation_artifact_paths", "required_page_ids", "sent_page_ids",
            "observed_page_ids", "render_failed_page_ids", "scan_failed_page_ids",
        ):
            payload[field_name] = list(payload[field_name])
        payload["transport_omissions"] = [dict(item) for item in self.transport_omissions]
        return payload

    def qualification_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        if self.artifact_type != "stage1_visual_evidence_qualification" or self.artifact_version != "v1":
            issues.append("qualification_type_invalid")
        if self.required_nonblank_page_count != len(self.required_page_ids):
            issues.append("required_page_ids_incomplete")
        required = set(self.required_page_ids)
        if self.scan_coverage_status in {"complete", "partial", "failed"}:
            if self.visual_observation_artifact_version != VISUAL_OBSERVATIONS_VERSION:
                issues.append("visual_observation_schema_invalid")
            if self.visual_scan_prompt_id != VISUAL_SCAN_PROMPT_ID:
                issues.append("visual_scan_prompt_invalid")
            if not self.visual_scan_prompt_version or not self.visual_scan_prompt_sha256:
                issues.append("visual_scan_prompt_identity_missing")
            if not self.visual_scan_schema_hash:
                issues.append("visual_scan_schema_hash_missing")
            if required - set(self.sent_page_ids):
                issues.append("required_page_inputs_not_sent")
            if required - set(self.observed_page_ids):
                issues.append("required_page_observations_missing")
        if self.render_failed_page_ids:
            issues.append("required_page_render_failed")
        if self.scan_failed_page_ids:
            issues.append("required_page_scan_failed")
        if self.transport_omissions:
            issues.append("required_visual_transport_omitted")
        if self.scan_coverage_status not in {"complete", "partial", "failed", "not_required"}:
            issues.append("scan_coverage_status_invalid")
        if self.final_synthesis_modality not in {"multimodal", "text_only", "pdf_plus_text"}:
            issues.append("final_synthesis_modality_invalid")
        if self.final_raw_visual_recheck_status not in {
            "complete", "partial", "not_run_fallback", "not_required",
        }:
            issues.append("final_raw_visual_recheck_status_invalid")
        if self.evidence_coverage_status not in {"complete", "degraded", "incomplete"}:
            issues.append("evidence_coverage_status_invalid")
        if self.require_complete_visual_coverage and self.evidence_coverage_status != "complete":
            issues.append("evidence_not_complete_for_reuse")
        return tuple(dict.fromkeys(issues))

    def complete_for_reuse(self) -> bool:
        return not self.qualification_issues()


@dataclass(frozen=True)
class Stage1ReusableSummaryBindingV1:
    """The source and execution facts that must match before reuse."""

    binding_version: str = STAGE1_REUSE_BINDING_VERSION
    canonical_paper_key: str = ""
    source_paper_id: str = ""
    source_mode: str = ""
    source_pdf: str = ""
    source_pdf_hash: str = ""
    source_pdf_fingerprint: str = ""
    source_pdf_content_sha256: str = ""
    stage1_extracted_text_hash: str = ""
    stage1_semantic_input_hash: str = ""
    preprocess_contract_hash: str = ""
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    prompt_template_hash: str = ""
    input_builder_policy_hash: str = ""
    summary_schema_hash: str = ""
    visual_input_manifest_hash: str = ""
    visual_coverage_hash: str = ""
    visual_scan_schema_hash: str = ""
    visual_evidence_qualification: Mapping[str, Any] = field(default_factory=dict)
    original_source_location: str = ""
    current_source_location: str = ""
    location_changed: bool = False
    preprocess_hash: str = ""
    stage1_input_hash: str = ""
    prompt_hash: str = ""
    builder_version: str = ""
    provider: str = ""
    model: str = ""
    endpoint_type: str = ""
    provider_config_hash: str = ""
    schema_hash: str = ""
    visual_provenance_hash: str = ""
    source_kind: str = ""
    evidence_manifest_id: str = ""
    evidence_manifest_hash: str = ""
    current_evidence_manifest_id: str = ""
    current_evidence_manifest_hash: str = ""
    runtime_spec_id: str = ""
    runtime_spec_hash: str = ""
    current_runtime_spec_id: str = ""
    current_runtime_spec_hash: str = ""
    expected_call_graph_id: str = ""
    expected_call_graph_hash: str = ""
    provider_receipt_closure_id: str = ""
    provider_receipt_closure_hash: str = ""
    source_provider_receipt_closure_id: str = ""
    source_provider_receipt_closure_hash: str = ""
    source_provider_receipt_ledger_id: str = ""
    source_provider_receipt_ledger_hash: str = ""
    normalized_summary_payload_hash: str = ""
    summary_payload_hash: str = ""
    registered_source_artifact_id: str = ""
    registered_source_artifact_hash: str = ""
    registered_source_artifact_path: str = ""
    registry_file_hash: str = ""
    source_summary_manifest_id: str = ""
    source_summary_manifest_hash: str = ""
    source_authority_job_id: str = ""
    source_authority_artifact_id: str = ""
    source_authority_artifact_hash: str = ""
    source_authority_artifact_path: str = ""
    source_authority_registry_id: str = ""
    source_authority_registry_revision: str = ""
    source_authority_closure_id: str = ""
    source_authority_closure_hash: str = ""
    current_snapshot_artifact_id: str = ""
    current_snapshot_artifact_hash: str = ""
    current_snapshot_artifact_path: str = ""
    source_authority_registry_path: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["extra"] = dict(self.extra)
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "Stage1ReusableSummaryBindingV1":
        raw = dict(value or {})
        nested = raw.get("binding")
        if isinstance(nested, Mapping):
            raw = dict(nested)
        known: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            if name in {"extra", "visual_evidence_qualification"} or name not in raw or raw[name] is None:
                continue
            known[name] = bool(raw[name]) if name == "location_changed" else _text(raw[name])
        if "visual_evidence_qualification" in raw and isinstance(
            raw.get("visual_evidence_qualification"), Mapping
        ):
            known["visual_evidence_qualification"] = (
                Stage1VisualEvidenceQualificationV1.from_mapping(
                    raw.get("visual_evidence_qualification")
                ).to_dict()
            )
        raw_extra_value = raw.get("extra")
        raw_extra: Mapping[str, Any] = (
            raw_extra_value if isinstance(raw_extra_value, Mapping) else {}
        )
        aliases = {
            # Do not alias the old semantic source_pdf_hash to the new byte
            # hash.  Old records remain legacy bindings and new records must
            # carry an explicit source_pdf_content_sha256.
            "source_pdf_content_sha256": (
                "source_pdf_content_hash",
                "pdf_content_hash",
                "source_pdf_file_hash",
            ),
            "stage1_semantic_input_hash": (
                "semantic_source_hash",
                "source_pdf_hash",
            ),
            "stage1_extracted_text_hash": ("stage1_input_text_hash",),
            "preprocess_contract_hash": ("preprocess_hash",),
            "prompt_template_hash": ("prompt_template_digest",),
            "input_builder_policy_hash": ("builder_policy_hash",),
            "summary_schema_hash": ("schema_hash",),
            "visual_input_manifest_hash": ("visual_provenance_hash", "visual_hash"),
            "visual_provenance_hash": ("visual_hash",),
            "registered_source_artifact_hash": ("source_artifact_hash",),
            "registered_source_artifact_path": ("source_artifact_path",),
            "normalized_summary_payload_hash": ("summary_payload_hash", "ai_summary_hash"),
            "summary_payload_hash": ("normalized_summary_payload_hash", "ai_summary_hash"),
            "source_authority_registry_id": ("source_registry_identity",),
            "source_authority_registry_revision": ("source_registry_revision",),
            "source_authority_closure_id": ("source_provider_receipt_closure_id",),
            "source_authority_closure_hash": ("source_provider_receipt_closure_hash",),
        }
        for target, candidates in aliases.items():
            if target in known:
                continue
            for candidate in candidates:
                if candidate in raw and raw[candidate] is not None:
                    known[target] = _text(raw[candidate])
                    break
        if "source_pdf_content_sha256" not in known:
            extra_file_hash = raw_extra.get("source_pdf_file_hash")
            if extra_file_hash:
                known["source_pdf_content_sha256"] = _text(extra_file_hash)
        if "source_kind" not in known:
            known["source_kind"] = _text(
                raw.get("source_kind") or raw_extra.get("source_kind")
            )
        known["extra"] = dict(raw_extra)
        return cls(**known)

    def _uses_structured_contract(self) -> bool:
        return any(
            _text(getattr(self, field_name))
            for field_name in (
                "source_pdf_content_sha256",
                "stage1_extracted_text_hash",
                "stage1_semantic_input_hash",
                "preprocess_contract_hash",
                "prompt_id",
                "prompt_version",
                "prompt_sha256",
                "prompt_template_hash",
                "input_builder_policy_hash",
                "summary_schema_hash",
                "visual_input_manifest_hash",
                "visual_coverage_hash",
                "visual_scan_schema_hash",
                "normalized_summary_payload_hash",
            )
        )

    def _comparison_fields(self, current: "Stage1ReusableSummaryBindingV1") -> tuple[str, ...]:
        if self._uses_structured_contract() or current._uses_structured_contract():
            return _STRUCTURED_COMPARISON_FIELDS
        return _LEGACY_COMPARISON_FIELDS

    def comparison_projection(
        self,
        current: Stage1ReusableSummaryBindingV1 | None = None,
    ) -> dict[str, str]:
        fields = self._comparison_fields(current or self)
        return {field_name: _text(getattr(self, field_name)) for field_name in fields}

    def compare(self, current: "Stage1ReusableSummaryBindingV1") -> dict[str, Any]:
        mismatches: dict[str, dict[str, str]] = {}
        missing: list[str] = []
        comparison_fields = self._comparison_fields(current)
        for field_name in comparison_fields:
            original = _text(getattr(self, field_name))
            actual = _text(getattr(current, field_name))
            # A structured binding may be only partially populated when it
            # comes from an older producer.  Two empty values mean that both
            # sides omit this optional/forward-compatible contract field; it
            # is not a mismatch.  A value present on only one side remains a
            # missing-field mismatch, which preserves fail-closed invalidation
            # when a newer producer adds a binding fact.
            if not original and not actual:
                continue
            if field_name in _PROVENANCE_COMPARISON_FIELDS and not actual:
                # The current binding is built before it can own a prior
                # authority.  The non-empty original value is verified by the
                # Registry/manifest verifier instead of being copied into the
                # current input identity.
                continue
            if not original or not actual:
                missing.append(field_name)
            elif original != actual:
                mismatches[field_name] = {"original": original, "current": actual}
        return {
            "equal": not mismatches and not missing,
            "compared_fields": list(comparison_fields),
            "missing_fields": missing,
            "mismatches": mismatches,
            "original": self.comparison_projection(current),
            "current": current.comparison_projection(self),
        }


@dataclass(frozen=True)
class Stage1ReusableSummaryManifestV1:
    """Typed authority manifest for one reusable Stage 1 summary."""

    artifact_type: str = "stage1_reusable_summary_manifest"
    artifact_version: str = "v1"
    job_id: str = ""
    stage_name: str = "stage1_analyze"
    canonical_paper_key: str = ""
    source_paper_id: str = ""
    source_summary_artifact_id: str = ""
    source_summary_artifact_hash: str = ""
    source_summary_artifact_path: str = ""
    source_summary_artifact_version: str = "v1"
    summary_payload_hash: str = ""
    normalized_summary_payload_hash: str = ""
    binding_hash: str = ""
    source_pdf_content_sha256: str = ""
    stage1_extracted_text_hash: str = ""
    stage1_semantic_input_hash: str = ""
    preprocess_contract_hash: str = ""
    prompt_id: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    prompt_template_hash: str = ""
    input_builder_policy_hash: str = ""
    summary_schema_hash: str = ""
    visual_input_manifest_hash: str = ""
    visual_coverage_hash: str = ""
    visual_scan_schema_hash: str = ""
    visual_evidence_qualification: Mapping[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    endpoint_type: str = ""
    provider_config_hash: str = ""
    summary_schema_version: str = ""
    provider_receipt_closure_id: str = ""
    provider_receipt_closure_hash: str = ""
    provider_receipt_closure_path: str = ""
    provider_receipt_ledger_id: str = ""
    provider_receipt_ledger_hash: str = ""
    provider_receipt_ledger_path: str = ""
    source_registry_identity: str = ""
    source_registry_revision: str = ""
    source_kind: str = ""
    manifest_content_hash: str = ""
    binding: Mapping[str, Any] = field(default_factory=dict)
    paper_info: Mapping[str, Any] = field(default_factory=dict)
    summary_payload: Mapping[str, Any] = field(default_factory=dict)
    runtime_spec_id: str = ""
    runtime_spec_hash: str = ""
    evidence_manifest_id: str = ""
    evidence_manifest_hash: str = ""
    source_bundle_id: str = ""
    source_bundle_hash: str = ""
    created_at: str = ""
    producer: str = "services.stage1_analysis_service.Stage1AnalysisService"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "Stage1ReusableSummaryManifestV1":
        raw = dict(value or {})
        known: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            if name not in raw:
                continue
            if name in {"binding", "paper_info", "summary_payload", "visual_evidence_qualification"}:
                known[name] = dict(raw[name]) if isinstance(raw[name], Mapping) else {}
            else:
                known[name] = _text(raw[name])
        return cls(**known)


@dataclass(frozen=True)
class Stage1TypedManifestAuthorityV1:
    """Verified portable authority material resolved from a typed manifest."""

    manifest: Stage1ReusableSummaryManifestV1
    manifest_path: str
    manifest_file_hash: str
    manifest_artifact_id: str
    source_summary_path: str
    provider_closure_path: str
    provider_ledger_path: str


def _is_sha256(value: Any) -> bool:
    text = _text(value)
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _binding_content_hash(payload: Mapping[str, Any]) -> str:
    normalized = dict(payload)
    # These fields identify the containing manifest record and therefore
    # cannot participate in the manifest's nested binding hash.
    normalized.pop("source_summary_manifest_id", None)
    normalized.pop("source_summary_manifest_hash", None)
    return hash_json(normalized)


def _manifest_content_hash(payload: Mapping[str, Any]) -> str:
    normalized = dict(payload)
    normalized["manifest_content_hash"] = ""
    return hash_json(normalized)


def _resolve_manifest_reference(manifest_path: Path, declared_path: str) -> Path | None:
    value = _text(declared_path)
    if not value:
        return None
    target = Path(value).expanduser()
    if not target.is_absolute():
        target = manifest_path.parent / target
    try:
        return target.resolve()
    except OSError:
        return None


def _typed_manifest_metadata(
    previous_summary: Mapping[str, Any],
) -> tuple[str, str, str]:
    metadata = _mapping(previous_summary.get("stage1_reuse"))
    if _text(metadata.get("authority_kind")) != "typed_manifest":
        return "", "", ""
    return (
        _text(metadata.get("typed_manifest_path")),
        _text(metadata.get("typed_manifest_artifact_id")),
        _text(metadata.get("typed_manifest_artifact_hash")),
    )


def _validate_manifest_self_binding(
    payload: Mapping[str, Any],
    *,
    binding: Stage1ReusableSummaryBindingV1,
    previous_summary: Mapping[str, Any],
) -> tuple[Stage1ReusableSummaryManifestV1 | None, str]:
    manifest = Stage1ReusableSummaryManifestV1.from_mapping(payload)
    if manifest.artifact_type != "stage1_reusable_summary_manifest":
        return None, "typed_manifest_type_invalid"
    if manifest.artifact_version != "v1":
        return None, "typed_manifest_version_invalid"
    if manifest.stage_name != "stage1_analyze" or not manifest.job_id:
        return None, "typed_manifest_stage_identity_invalid"
    if not manifest.canonical_paper_key:
        return None, "typed_manifest_paper_identity_missing"
    if not _is_sha256(manifest.manifest_content_hash):
        return None, "typed_manifest_content_hash_missing"
    if _manifest_content_hash(payload) != manifest.manifest_content_hash:
        return None, "typed_manifest_content_hash_mismatch"
    if not manifest.binding or not _is_sha256(manifest.binding_hash):
        return None, "typed_manifest_binding_missing"
    if _binding_content_hash(manifest.binding) != manifest.binding_hash:
        return None, "typed_manifest_binding_hash_mismatch"
    if _binding_content_hash(binding.to_dict()) != manifest.binding_hash:
        return None, "typed_manifest_imported_binding_mismatch"

    manifest_binding = Stage1ReusableSummaryBindingV1.from_mapping(manifest.binding)
    top_level_binding_fields = (
        "canonical_paper_key",
        "source_pdf_content_sha256",
        "stage1_extracted_text_hash",
        "stage1_semantic_input_hash",
        "preprocess_contract_hash",
        "prompt_id",
        "prompt_version",
        "prompt_sha256",
        "prompt_template_hash",
        "input_builder_policy_hash",
        "summary_schema_hash",
        "visual_input_manifest_hash",
        "visual_scan_schema_hash",
        "provider",
        "model",
        "endpoint_type",
        "provider_config_hash",
    )
    for field_name in top_level_binding_fields:
        manifest_value = _text(getattr(manifest, field_name))
        binding_value = _text(getattr(manifest_binding, field_name))
        if (manifest_value or binding_value) and (
            not manifest_value or manifest_value != binding_value
        ):
            return None, f"typed_manifest_{field_name}_mismatch"
    manifest_qualification = Stage1VisualEvidenceQualificationV1.from_mapping(
        manifest.visual_evidence_qualification
    ).to_dict()
    binding_qualification = Stage1VisualEvidenceQualificationV1.from_mapping(
        manifest_binding.visual_evidence_qualification
    ).to_dict()
    if manifest_qualification != binding_qualification:
        return None, "typed_manifest_visual_evidence_qualification_mismatch"
    if manifest.source_registry_identity != manifest_binding.source_authority_registry_id:
        return None, "typed_manifest_registry_identity_mismatch"
    if manifest.source_registry_revision != manifest_binding.source_authority_registry_revision:
        return None, "typed_manifest_registry_revision_mismatch"
    if manifest.source_summary_artifact_id != manifest_binding.source_authority_artifact_id:
        return None, "typed_manifest_source_artifact_id_mismatch"
    if manifest.source_summary_artifact_hash != manifest_binding.source_authority_artifact_hash:
        return None, "typed_manifest_source_artifact_hash_mismatch"
    if manifest.provider_receipt_closure_id != manifest_binding.source_provider_receipt_closure_id:
        return None, "typed_manifest_provider_closure_id_mismatch"
    if manifest.provider_receipt_closure_hash != manifest_binding.source_provider_receipt_closure_hash:
        return None, "typed_manifest_provider_closure_hash_mismatch"
    if manifest.provider_receipt_ledger_id != manifest_binding.source_provider_receipt_ledger_id:
        return None, "typed_manifest_provider_ledger_id_mismatch"
    if manifest.provider_receipt_ledger_hash != manifest_binding.source_provider_receipt_ledger_hash:
        return None, "typed_manifest_provider_ledger_hash_mismatch"

    imported_summary = previous_summary.get("ai_summary")
    if not isinstance(imported_summary, Mapping) or not manifest.summary_payload:
        return None, "typed_manifest_summary_payload_missing"
    summary_hash = hash_json(manifest.summary_payload)
    if summary_hash != hash_json(imported_summary):
        return None, "typed_manifest_imported_summary_payload_mismatch"
    for declared_hash in (
        manifest.summary_payload_hash,
        manifest.normalized_summary_payload_hash,
        manifest_binding.summary_payload_hash,
        manifest_binding.normalized_summary_payload_hash,
    ):
        if not _is_sha256(declared_hash) or declared_hash != summary_hash:
            return None, "typed_manifest_summary_payload_hash_mismatch"
    paper_info = _mapping(previous_summary.get("paper_info"))
    if _text(paper_info.get("canonical_paper_key")) != manifest.canonical_paper_key:
        return None, "typed_manifest_imported_paper_identity_mismatch"
    if _text(manifest.paper_info.get("canonical_paper_key")) != manifest.canonical_paper_key:
        return None, "typed_manifest_paper_payload_identity_mismatch"
    return manifest, "typed_manifest_self_binding_verified"


def _verify_visual_evidence_qualification(
    raw_qualification: Mapping[str, Any] | None,
    *,
    manifest_path: Path | None = None,
    registry: ArtifactRegistry | None = None,
) -> tuple[bool, str]:
    """Verify visual coverage/observation bytes and the achieved reducer."""

    if not raw_qualification:
        # Bindings written before the typed qualification was introduced are
        # accepted only when they never opted into the complete-coverage gate.
        # Current Stage 1 bindings always carry the policy explicitly.
        return True, "visual_qualification_not_declared_legacy"
    qualification = Stage1VisualEvidenceQualificationV1.from_mapping(raw_qualification)
    issues = qualification.qualification_issues()
    if issues:
        if any(issue in issues for issue in ("required_page_observations_missing", "required_page_inputs_not_sent")):
            return False, "prior_visual_observation_incomplete"
        if any(issue in issues for issue in ("required_page_render_failed", "required_page_scan_failed", "required_visual_transport_omitted")):
            return False, "prior_visual_coverage_incomplete"
        if "evidence_not_complete_for_reuse" in issues:
            return False, "prior_visual_coverage_incomplete"
        if any(
            issue in issues
            for issue in (
                "visual_observation_schema_invalid",
                "visual_scan_prompt_invalid",
                "visual_scan_prompt_identity_missing",
                "visual_scan_schema_hash_missing",
            )
        ):
            return False, "prior_visual_observation_contract_invalid"
        return False, "prior_visual_coverage_artifact_invalid"

    required_visual_evidence = bool(
        qualification.required_page_ids
        or qualification.required_nonblank_page_count
        or qualification.scan_coverage_status != "not_required"
        or qualification.observation_artifact_ids
    )
    expected_scan_identity = None
    expected_scan_schema_hash = ""
    if required_visual_evidence:
        try:
            expected_scan_identity = PromptRegistry().identity(VISUAL_SCAN_PROMPT_ID)
            expected_scan_schema_hash = hash_json(
                {
                    "artifact_type": "stage1_visual_observations",
                    "artifact_version": VISUAL_OBSERVATIONS_VERSION,
                    "prompt_id": expected_scan_identity.prompt_id,
                    "prompt_version": expected_scan_identity.version,
                    "prompt_sha256": expected_scan_identity.sha256,
                }
            )
        except (OSError, TypeError, ValueError, RuntimeError):
            return False, "prior_visual_observation_contract_invalid"
        if (
            qualification.visual_observation_artifact_version != VISUAL_OBSERVATIONS_VERSION
            or qualification.visual_scan_prompt_id != expected_scan_identity.prompt_id
            or qualification.visual_scan_prompt_version != expected_scan_identity.version
            or qualification.visual_scan_prompt_sha256 != expected_scan_identity.sha256
            or qualification.visual_scan_schema_hash != expected_scan_schema_hash
        ):
            return False, "prior_visual_observation_contract_invalid"

    def resolve_path(declared: str) -> Path | None:
        if not declared:
            return None
        target = Path(declared).expanduser()
        if not target.is_absolute() and manifest_path is not None:
            target = manifest_path.parent / target
        try:
            return target.resolve()
        except OSError:
            return None

    def verify_artifact(
        *,
        artifact_id: str,
        expected_hash: str,
        declared_path: str,
        artifact_type: str,
        expected_artifact_version: str,
        invalid_reason: str,
    ) -> tuple[bool, Path | None, str]:
        record = registry.get(artifact_id) if registry is not None and artifact_id else None
        if record is not None:
            if (
                record.status != "ready"
                or record.artifact_type != artifact_type
                or record.artifact_version != expected_artifact_version
            ):
                return False, None, invalid_reason
            if expected_hash and record.content_hash != expected_hash:
                return False, None, invalid_reason
            target = Path(record.path).expanduser()
        else:
            target = resolve_path(declared_path)
            if target is None:
                return False, None, invalid_reason
        if not target.is_file() or not expected_hash or not _is_sha256(expected_hash):
            return False, None, invalid_reason
        actual_hash = _hash_file(str(target))
        if actual_hash != expected_hash:
            return False, None, invalid_reason
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, None, invalid_reason
        if (
            not isinstance(payload, Mapping)
            or payload.get("artifact_type") != artifact_type
            or payload.get("artifact_version") != expected_artifact_version
        ):
            return False, None, invalid_reason
        if registry is not None and record is not None:
            try:
                ArtifactRegistry._verify_ready_artifact(record)
            except (OSError, TypeError, ValueError, RuntimeError):
                return False, None, invalid_reason
        return True, target, ""

    if required_visual_evidence:
        coverage_ok, coverage_path, coverage_reason = verify_artifact(
            artifact_id=qualification.coverage_artifact_id,
            expected_hash=qualification.coverage_artifact_hash,
            declared_path=qualification.coverage_artifact_path,
            artifact_type="stage1_visual_coverage",
            expected_artifact_version="v1",
            invalid_reason="prior_visual_coverage_artifact_invalid",
        )
        if not coverage_ok:
            return False, coverage_reason
        try:
            coverage_payload = json.loads(coverage_path.read_text(encoding="utf-8")) if coverage_path else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, "prior_visual_coverage_artifact_invalid"
        if not isinstance(coverage_payload, Mapping):
            return False, "prior_visual_coverage_artifact_invalid"
        for field_name in (
            "scan_coverage_status", "required_nonblank_page_count",
            "required_page_ids", "sent_visual_ids", "observed_visual_ids",
        ):
            declared = getattr(qualification, field_name, None)
            if field_name == "sent_visual_ids":
                declared = list(qualification.sent_page_ids)
            elif field_name == "observed_visual_ids":
                declared = list(qualification.observed_page_ids)
            elif field_name == "required_page_ids":
                declared = list(qualification.required_page_ids)
            if declared is not None and field_name in coverage_payload:
                actual = coverage_payload.get(field_name)
                if field_name == "required_nonblank_page_count":
                    actual_count = _as_int_or_none(actual)
                    declared_count = _as_int_or_none(declared)
                    if actual_count is None or declared_count is None or actual_count != declared_count:
                        return False, "prior_visual_coverage_artifact_invalid"
                elif field_name == "scan_coverage_status":
                    if str(actual or "") != str(declared or ""):
                        return False, "prior_visual_coverage_artifact_invalid"
                elif field_name in {"sent_visual_ids", "observed_visual_ids"}:
                    # The qualification records page IDs, while the durable
                    # coverage artifact may also contain child crops.  The
                    # page proof must be present; extra crop IDs are valid
                    # and must not make an equivalent artifact unverifiable.
                    actual_ids = {str(item) for item in (actual or [])}
                    declared_ids = {str(item) for item in (declared or [])}
                    if not declared_ids.issubset(actual_ids):
                        return False, "prior_visual_coverage_artifact_invalid"
                elif sorted(str(item) for item in (actual or [])) != sorted(str(item) for item in (declared or [])):
                    return False, "prior_visual_coverage_artifact_invalid"

    if len(qualification.observation_artifact_ids) != len(qualification.observation_artifact_hashes):
        return False, "prior_visual_observation_artifact_invalid"
    if qualification.observation_artifact_paths and len(qualification.observation_artifact_paths) != len(qualification.observation_artifact_ids):
        return False, "prior_visual_observation_artifact_invalid"
    for index, (artifact_id, artifact_hash) in enumerate(
        zip(qualification.observation_artifact_ids, qualification.observation_artifact_hashes)
    ):
        declared_path = (
            qualification.observation_artifact_paths[index]
            if index < len(qualification.observation_artifact_paths)
            else ""
        )
        ok, observation_path, reason = verify_artifact(
            artifact_id=artifact_id,
            expected_hash=artifact_hash,
            declared_path=declared_path,
            artifact_type="stage1_visual_observations",
            expected_artifact_version=VISUAL_OBSERVATIONS_VERSION,
            invalid_reason="prior_visual_observation_artifact_invalid",
        )
        if not ok:
            return False, reason
        try:
            observation_payload = (
                json.loads(observation_path.read_text(encoding="utf-8"))
                if observation_path is not None
                else {}
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, "prior_visual_observation_artifact_invalid"
        if not isinstance(observation_payload, Mapping):
            return False, "prior_visual_observation_artifact_invalid"
        if (
            observation_payload.get("prompt_id") != qualification.visual_scan_prompt_id
            or observation_payload.get("prompt_version") != qualification.visual_scan_prompt_version
            or observation_payload.get("prompt_sha256") != qualification.visual_scan_prompt_sha256
        ):
            return False, "prior_visual_observation_contract_invalid"
    if qualification.require_complete_visual_coverage and not qualification.complete_for_reuse():
        return False, "prior_visual_coverage_incomplete"
    return True, (
        "visual_qualification_verified"
        if qualification.evidence_coverage_status == "complete"
        else "degraded_visual_reuse_allowed_by_policy"
    )


def verify_stage1_typed_manifest_authority(
    previous_summary: Mapping[str, Any],
    binding: Stage1ReusableSummaryBindingV1,
) -> tuple[Stage1TypedManifestAuthorityV1 | None, str]:
    """Verify a portable manifest and every authority byte it binds."""

    manifest_path_text, manifest_artifact_id, expected_manifest_file_hash = (
        _typed_manifest_metadata(previous_summary)
    )
    if not manifest_path_text:
        return None, "typed_manifest_path_missing"
    manifest_path = Path(manifest_path_text).expanduser()
    try:
        manifest_path = manifest_path.resolve()
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"typed_manifest_unreadable:{exc}"
    if not isinstance(raw_manifest, Mapping):
        return None, "typed_manifest_payload_invalid"
    manifest_file_hash = _hash_file(str(manifest_path))
    if (
        not manifest_artifact_id
        or not _is_sha256(expected_manifest_file_hash)
        or manifest_file_hash != expected_manifest_file_hash
    ):
        return None, "typed_manifest_file_hash_mismatch"
    manifest, reason = _validate_manifest_self_binding(
        raw_manifest,
        binding=binding,
        previous_summary=previous_summary,
    )
    if manifest is None:
        return None, reason

    qualification_ok, qualification_reason = _verify_visual_evidence_qualification(
        manifest.visual_evidence_qualification,
        manifest_path=manifest_path,
    )
    if not qualification_ok:
        return None, qualification_reason

    source_summary_path = _resolve_manifest_reference(
        manifest_path, manifest.source_summary_artifact_path
    )
    if source_summary_path is None or not source_summary_path.is_file():
        return None, "typed_manifest_source_summary_missing"
    if _hash_file(str(source_summary_path)) != manifest.source_summary_artifact_hash:
        return None, "typed_manifest_source_summary_hash_mismatch"
    payload_ok, payload_reason = _authority_summary_matches(
        path=str(source_summary_path),
        canonical_paper_key=manifest.canonical_paper_key,
        previous_summary=previous_summary,
        binding=binding,
    )
    if not payload_ok:
        return None, payload_reason.replace("registered_source", "typed_manifest_source")

    provider_generated = manifest.source_kind in {
        "stage1_provider_generated",
        "provider_generated",
        "runtime_stage1",
    }
    closure_path = _resolve_manifest_reference(
        manifest_path, manifest.provider_receipt_closure_path
    )
    if provider_generated and (
        closure_path is None
        or not closure_path.is_file()
        or not _is_sha256(manifest.provider_receipt_closure_hash)
        or _hash_file(str(closure_path)) != manifest.provider_receipt_closure_hash
    ):
        return None, "typed_manifest_provider_closure_untrusted"

    closure_payload: Mapping[str, Any] = {}
    if closure_path is not None and closure_path.is_file():
        try:
            raw_closure = json.loads(closure_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return None, f"typed_manifest_provider_closure_unreadable:{exc}"
        if not isinstance(raw_closure, Mapping):
            return None, "typed_manifest_provider_closure_payload_invalid"
        if (
            raw_closure.get("artifact_type") != "provider_receipt_closure"
            or raw_closure.get("artifact_version") != "v1"
            or _text(raw_closure.get("job_id")) != manifest.job_id
            or _text(raw_closure.get("stage_name")) != "stage1_analyze"
        ):
            return None, "typed_manifest_provider_closure_identity_invalid"
        nested_closure = raw_closure.get("payload")
        if not isinstance(nested_closure, Mapping) or nested_closure.get("complete") is not True:
            return None, "typed_manifest_provider_closure_incomplete"
        closure_payload = raw_closure

    expected_calls = closure_payload.get("expected_calls")
    expected_calls = expected_calls if isinstance(expected_calls, list) else []
    ledger_path = _resolve_manifest_reference(
        manifest_path, manifest.provider_receipt_ledger_path
    )
    if expected_calls:
        if (
            ledger_path is None
            or not ledger_path.is_file()
            or not _is_sha256(manifest.provider_receipt_ledger_hash)
            or _hash_file(str(ledger_path)) != manifest.provider_receipt_ledger_hash
        ):
            return None, "typed_manifest_provider_ledger_untrusted"
        try:
            from runtime.provider_receipt_closure import ProviderReceiptClosure
            from runtime.provider_runtime import ProviderRuntimeLedger

            receipts = ProviderRuntimeLedger(str(ledger_path)).list_receipts()
            recomputed = ProviderReceiptClosure.evaluate(expected_calls, receipts)
        except (OSError, UnicodeError, TypeError, ValueError, RuntimeError) as exc:
            return None, f"typed_manifest_provider_ledger_invalid:{exc}"
        declared_closure = _mapping(closure_payload.get("payload"))
        if not recomputed.complete or recomputed.to_dict() != dict(declared_closure):
            return None, "typed_manifest_provider_closure_recompute_mismatch"
    elif provider_generated:
        return None, "typed_manifest_provider_expected_calls_missing"

    return (
        Stage1TypedManifestAuthorityV1(
            manifest=manifest,
            manifest_path=str(manifest_path),
            manifest_file_hash=manifest_file_hash,
            manifest_artifact_id=manifest_artifact_id,
            source_summary_path=str(source_summary_path),
            provider_closure_path=str(closure_path or ""),
            provider_ledger_path=str(ledger_path or ""),
        ),
        "typed_manifest_authority_verified",
    )


@dataclass(frozen=True)
class Stage1ReuseEligibilityV1:
    decision: str
    canonical_paper_key: str
    reason: str
    original_source_binding: Mapping[str, Any]
    current_source_binding: Mapping[str, Any]
    reuse_comparison: Mapping[str, Any]

    @property
    def reusable(self) -> bool:
        return self.decision in {"exact_summary_reuse", "reusable"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "stage1_reuse_eligibility",
            "artifact_version": STAGE1_REUSE_BINDING_VERSION,
            "policy": STAGE1_REUSE_POLICY,
            "decision": self.decision,
            "reusable": self.reusable,
            "canonical_paper_key": self.canonical_paper_key,
            "reason": self.reason,
            "original_source_binding": dict(self.original_source_binding),
            "current_source_binding": dict(self.current_source_binding),
            "reuse_comparison": dict(self.reuse_comparison),
        }


def _summary_candidates(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    return []


def _authority_summary_matches(
    *,
    path: str,
    canonical_paper_key: str,
    previous_summary: Mapping[str, Any],
    binding: Stage1ReusableSummaryBindingV1,
) -> tuple[bool, str]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"registered_source_artifact_payload_unreadable:{exc}"
    imported_summary = previous_summary.get("ai_summary")
    if not isinstance(imported_summary, Mapping):
        return False, "registered_source_artifact_payload_import_missing"
    candidates = _summary_candidates(payload)
    matching: Mapping[str, Any] | None = None
    for candidate in candidates:
        paper_info = candidate.get("paper_info")
        candidate_key = (
            str(paper_info.get("canonical_paper_key") or "")
            if isinstance(paper_info, Mapping)
            else str(candidate.get("canonical_paper_key") or "")
        )
        if candidate_key == canonical_paper_key:
            matching = candidate
            break
    if matching is None and len(candidates) == 1:
        matching = candidates[0]
    if matching is None:
        return False, "registered_source_artifact_payload_identity_missing"
    authoritative_summary = matching.get("ai_summary")
    if not isinstance(authoritative_summary, Mapping):
        analysis = matching.get("analysis")
        if isinstance(analysis, Mapping) and isinstance(analysis.get("ai_summary"), Mapping):
            authoritative_summary = analysis.get("ai_summary")
        else:
            authoritative_summary = analysis
    if not isinstance(authoritative_summary, Mapping):
        return False, "registered_source_artifact_payload_summary_missing"
    authoritative_hash = hash_json(authoritative_summary)
    imported_hash = hash_json(imported_summary)
    if authoritative_hash != imported_hash:
        return False, "registered_source_artifact_payload_mismatch"
    declared_hash = str(
        matching.get("summary_payload_hash")
        or matching.get("normalized_summary_payload_hash")
        or matching.get("ai_summary_hash")
        or ""
    ).strip()
    if not declared_hash or declared_hash != authoritative_hash:
        return False, "registered_source_artifact_payload_hash_mismatch"
    bound_hash = _text(
        binding.normalized_summary_payload_hash or binding.summary_payload_hash
    )
    if not bound_hash or bound_hash != authoritative_hash:
        return False, "registered_source_artifact_summary_payload_hash_mismatch"
    return True, "registered_source_artifact_payload_verified"


def _registered_source_is_verifiable(
    binding: Stage1ReusableSummaryBindingV1,
    previous_summary: Mapping[str, Any],
    *,
    registry: ArtifactRegistry | None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> tuple[bool, str]:
    manifest_path, _manifest_id, _manifest_hash = _typed_manifest_metadata(
        previous_summary
    )
    if manifest_path:
        authority, reason = verify_stage1_typed_manifest_authority(
            previous_summary,
            binding,
        )
        return authority is not None, reason

    # A current-run snapshot or a summary-declared path is derived evidence,
    # never the source of authority.  Require the explicit parent identity.
    authority_id = _text(binding.source_authority_artifact_id)
    authority_job_id = _text(binding.source_authority_job_id)
    if not authority_id:
        return False, "source_authority_artifact_id_missing"
    if not authority_job_id:
        return False, "source_authority_job_id_missing"

    target_registry = (
        registry if registry is not None and authority_job_id == registry.job_id else None
    )
    if target_registry is None:
        if external_registry_resolver is None:
            # A registry path is a locator for an already typed authority, not
            # a fallback authority resolver.
            return False, "source_authority_registry_resolver_missing"
        target_registry = external_registry_resolver(authority_job_id)
        if target_registry is None:
            return False, "source_authority_registry_unavailable"
        target_registry.reload()
    qualification_ok, qualification_reason = _verify_visual_evidence_qualification(
        binding.visual_evidence_qualification,
        registry=target_registry,
    )
    if not qualification_ok:
        return False, qualification_reason
    record = target_registry.get(authority_id)
    if record is None:
        return False, "source_authority_artifact_not_registered"
    if record.status != "ready":
        return False, "source_authority_artifact_not_ready"
    if record.job_id != authority_job_id:
        return False, "source_authority_job_mismatch"
    expected_content_hash = _text(binding.source_authority_artifact_hash)
    if not expected_content_hash:
        return False, "source_authority_artifact_hash_missing"
    if record.content_hash != expected_content_hash:
        return False, "source_authority_artifact_hash_mismatch"
    declared_path = _text(binding.source_authority_artifact_path)
    if declared_path and Path(declared_path).resolve() != Path(record.path).resolve():
        return False, "source_authority_artifact_path_mismatch"
    try:
        ArtifactRegistry._verify_ready_artifact(record)
        target_registry.verify_ready_dependencies(
            record.depends_on,
            external_registry_resolver=external_registry_resolver,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return False, f"source_authority_artifact_untrusted:{exc}"
    actual_hash = _hash_file(record.path)
    if not actual_hash:
        return False, "source_authority_artifact_missing"
    if actual_hash != record.content_hash:
        return False, "source_authority_artifact_content_hash_mismatch"
    expected_file_hash = _text(binding.registry_file_hash)
    if expected_file_hash and actual_hash != expected_file_hash:
        return False, "source_authority_artifact_file_hash_mismatch"

    # Verify the bytes and logical payload before reporting a missing registry
    # identity.  This keeps a malformed imported binding from masking the
    # more useful evidence failure and, importantly, never makes the payload
    # itself an authority.
    payload_ok, payload_reason = _authority_summary_matches(
        path=record.path,
        canonical_paper_key=_text(
            _mapping(previous_summary.get("paper_info")).get("canonical_paper_key")
            or binding.canonical_paper_key
        ),
        previous_summary=previous_summary,
        binding=binding,
    )
    if not payload_ok:
        return False, payload_reason

    # Surface the mandatory provider-closure failure before secondary
    # authority metadata failures.  A provider-generated summary without its
    # original closure is never eligible for reuse, even if an imported
    # Registry identity is malformed.
    early_source_kind = _text(binding.source_kind) or _text(
        _mapping(binding.extra).get("source_kind")
    )
    early_provider = _mapping(previous_summary.get("provider"))
    early_raw_count = (
        _mapping(binding.extra).get("provider_transport_count")
        or early_provider.get("transport_count")
        or len(early_provider.get("receipt_ids") or [])
        or 0
    )
    try:
        early_provider_count = int(early_raw_count)
    except (TypeError, ValueError):
        early_provider_count = 0
    if (
        early_source_kind in {"stage1_provider_generated", "provider_generated", "runtime_stage1"}
        or early_provider_count > 0
    ) and not (
        _text(binding.source_provider_receipt_closure_id)
        and _text(binding.source_provider_receipt_closure_hash)
    ):
        return False, "source_provider_receipt_closure_missing"

    expected_registry_id = f"artifact-registry:{authority_job_id}"
    if _text(binding.source_authority_registry_id) != expected_registry_id:
        return False, "source_authority_registry_identity_mismatch"
    if not _text(binding.source_authority_registry_revision):
        return False, "source_authority_registry_revision_missing"

    manifest_id = _text(binding.source_summary_manifest_id)
    manifest_hash = _text(binding.source_summary_manifest_hash)
    if not manifest_id or not manifest_hash:
        return False, "source_summary_manifest_binding_missing"
    manifest_record = target_registry.get(manifest_id)
    if manifest_record is None or manifest_record.status != "ready":
        return False, "source_summary_manifest_not_registered"
    if (
        manifest_record.artifact_type != "stage1_reusable_summary_manifest"
        or manifest_record.artifact_version != "v1"
    ):
        return False, "source_summary_manifest_type_invalid"
    if manifest_record.job_id != authority_job_id:
        return False, "source_summary_manifest_job_mismatch"
    if manifest_record.content_hash != manifest_hash:
        return False, "source_summary_manifest_hash_mismatch"
    if _hash_file(manifest_record.path) != manifest_record.content_hash:
        return False, "source_summary_manifest_file_hash_mismatch"
    try:
        manifest_payload = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"source_summary_manifest_unreadable:{exc}"
    if not isinstance(manifest_payload, Mapping):
        return False, "source_summary_manifest_payload_invalid"
    self_bound_manifest, self_bound_reason = _validate_manifest_self_binding(
        manifest_payload,
        binding=binding,
        previous_summary=previous_summary,
    )
    if self_bound_manifest is None:
        return False, f"source_summary_manifest_{self_bound_reason}"
    if manifest_payload.get("artifact_type") != "stage1_reusable_summary_manifest":
        return False, "source_summary_manifest_payload_type_invalid"
    if manifest_payload.get("artifact_version") != "v1":
        return False, "source_summary_manifest_payload_version_invalid"
    if str(manifest_payload.get("job_id") or "") != authority_job_id:
        return False, "source_summary_manifest_payload_job_mismatch"
    if str(manifest_payload.get("canonical_paper_key") or "") != _text(binding.canonical_paper_key):
        return False, "source_summary_manifest_paper_mismatch"
    if str(manifest_payload.get("source_summary_artifact_id") or "") != authority_id:
        return False, "source_summary_manifest_source_mismatch"
    if str(manifest_payload.get("source_summary_artifact_hash") or "") != expected_content_hash:
        return False, "source_summary_manifest_source_hash_mismatch"
    if (
        str(manifest_payload.get("source_registry_identity") or "")
        != _text(binding.source_authority_registry_id)
    ):
        return False, "source_summary_manifest_registry_identity_mismatch"
    if (
        str(manifest_payload.get("source_registry_revision") or "")
        != _text(binding.source_authority_registry_revision)
    ):
        return False, "source_summary_manifest_registry_revision_mismatch"
    for field_name in (
        "source_pdf_content_sha256",
        "stage1_extracted_text_hash",
        "stage1_semantic_input_hash",
        "preprocess_contract_hash",
        "prompt_id",
        "prompt_version",
        "prompt_sha256",
        "prompt_template_hash",
        "input_builder_policy_hash",
        "summary_schema_hash",
        "visual_input_manifest_hash",
        "visual_coverage_hash",
    ):
        if str(manifest_payload.get(field_name) or "") != _text(getattr(binding, field_name)):
            return False, f"source_summary_manifest_{field_name}_mismatch"
    manifest_payload_hash = _text(
        manifest_payload.get("normalized_summary_payload_hash")
        or manifest_payload.get("summary_payload_hash")
    )
    binding_payload_hash = _text(
        binding.normalized_summary_payload_hash or binding.summary_payload_hash
    )
    if not manifest_payload_hash or not binding_payload_hash:
        return False, "source_summary_manifest_payload_hash_missing"
    if manifest_payload_hash != binding_payload_hash:
        return False, "source_summary_manifest_payload_hash_mismatch"
    try:
        target_registry.verify_ready_dependencies(
            manifest_record.depends_on,
            external_registry_resolver=external_registry_resolver,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return False, f"source_summary_manifest_dependencies_untrusted:{exc}"

    source_kind = _text(binding.source_kind) or _text(_mapping(binding.extra).get("source_kind"))
    provider = _mapping(previous_summary.get("provider"))
    raw_count = (
        _mapping(binding.extra).get("provider_transport_count")
        or provider.get("transport_count")
        or len(provider.get("receipt_ids") or [])
        or 0
    )
    try:
        provider_count = int(raw_count)
    except (TypeError, ValueError):
        provider_count = 0
    provider_generated = source_kind in {
        "stage1_provider_generated",
        "provider_generated",
        "runtime_stage1",
    } or provider_count > 0
    closure_id = _text(binding.source_provider_receipt_closure_id)
    closure_hash = _text(binding.source_provider_receipt_closure_hash)
    ledger_id = _text(binding.source_provider_receipt_ledger_id)
    ledger_hash = _text(binding.source_provider_receipt_ledger_hash)
    manifest_closure_id = _text(manifest_payload.get("provider_receipt_closure_id"))
    manifest_closure_hash = _text(manifest_payload.get("provider_receipt_closure_hash"))
    manifest_ledger_id = _text(manifest_payload.get("provider_receipt_ledger_id"))
    manifest_ledger_hash = _text(manifest_payload.get("provider_receipt_ledger_hash"))
    if provider_generated and (not closure_id or not closure_hash):
        return False, "source_provider_receipt_closure_missing"
    if provider_generated and (closure_id != manifest_closure_id or closure_hash != manifest_closure_hash):
        return False, "source_provider_receipt_closure_manifest_mismatch"
    if ledger_id or ledger_hash:
        if not ledger_id or not ledger_hash:
            return False, "source_provider_receipt_ledger_binding_incomplete"
        if ledger_id != manifest_ledger_id or ledger_hash != manifest_ledger_hash:
            return False, "source_provider_receipt_ledger_manifest_mismatch"

    closure_record = target_registry.get(closure_id) if closure_id else None
    if provider_generated and closure_record is None:
        return False, "source_provider_receipt_closure_not_registered"
    if closure_record is not None:
        if (
            closure_record.status != "ready"
            or closure_record.artifact_type != "provider_receipt_closure"
            or closure_record.artifact_version != "v1"
        ):
            return False, "source_provider_receipt_closure_type_invalid"
        if closure_record.content_hash != closure_hash or _hash_file(closure_record.path) != closure_hash:
            return False, "source_provider_receipt_closure_hash_mismatch"
        try:
            ArtifactRegistry._verify_ready_artifact(closure_record)
            target_registry.verify_ready_dependencies(
                closure_record.depends_on,
                external_registry_resolver=external_registry_resolver,
            )
            closure_payload = json.loads(Path(closure_record.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
            return False, f"source_provider_receipt_closure_untrusted:{exc}"
        if not isinstance(closure_payload, Mapping) or str(closure_payload.get("job_id") or "") != authority_job_id:
            return False, "source_provider_receipt_closure_job_mismatch"
        closure_result = closure_payload.get("payload")
        if not isinstance(closure_result, Mapping) or closure_result.get("complete") is not True:
            return False, "source_provider_receipt_closure_incomplete"
        expected_count = len(closure_result.get("expected_call_ids") or [])
        if expected_count > 0 and (not ledger_id or not ledger_hash):
            return False, "source_provider_receipt_ledger_missing"
    if ledger_id:
        ledger_record = target_registry.get(ledger_id)
        if ledger_record is None or ledger_record.status != "ready":
            return False, "source_provider_receipt_ledger_not_registered"
        if (
            ledger_record.artifact_type != "provider_receipt_ledger"
            or ledger_record.artifact_version != "v1"
        ):
            return False, "source_provider_receipt_ledger_type_invalid"
        if ledger_record.content_hash != ledger_hash or _hash_file(ledger_record.path) != ledger_hash:
            return False, "source_provider_receipt_ledger_hash_mismatch"
        try:
            ArtifactRegistry._verify_ready_artifact(ledger_record)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            return False, f"source_provider_receipt_ledger_untrusted:{exc}"

    return True, "registered_source_authority_verified"


def evaluate_stage1_reuse(
    previous_summary: Mapping[str, Any],
    current_binding: Stage1ReusableSummaryBindingV1,
    *,
    registry: ArtifactRegistry | None = None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> Stage1ReuseEligibilityV1:
    """Evaluate a prior summary without creating a current-run authority."""

    paper_info = _mapping(previous_summary.get("paper_info"))
    canonical_key = _text(
        paper_info.get("canonical_paper_key") or current_binding.canonical_paper_key
    )
    metadata = _mapping(previous_summary.get("stage1_reuse"))
    raw_binding = metadata.get("binding")
    if not isinstance(raw_binding, Mapping):
        raw_binding = previous_summary.get("stage1_provenance")
    if not isinstance(raw_binding, Mapping):
        raw_binding = previous_summary.get("provenance")
    if not isinstance(raw_binding, Mapping):
        return Stage1ReuseEligibilityV1(
            decision="identity_match_unverified",
            canonical_paper_key=canonical_key,
            reason="prior_summary_has_no_registered_provenance_binding",
            original_source_binding={},
            current_source_binding=current_binding.to_dict(),
            reuse_comparison={"equal": False, "missing_fields": ["binding"]},
        )

    original = Stage1ReusableSummaryBindingV1.from_mapping(raw_binding)
    comparison = original.compare(current_binding)
    if not comparison["equal"]:
        source_changed = any(
            field_name in comparison.get("mismatches", {})
            for field_name in (
                "source_pdf_content_sha256",
                "stage1_extracted_text_hash",
                "stage1_semantic_input_hash",
                "preprocess_contract_hash",
                "source_pdf_hash",
                "source_pdf_fingerprint",
                "preprocess_hash",
            )
        )
        return Stage1ReuseEligibilityV1(
            decision="identity_match_but_stale" if source_changed else "binding_mismatch",
            canonical_paper_key=canonical_key,
            reason="registered_prior_binding_does_not_match_current_source",
            original_source_binding=original.to_dict(),
            current_source_binding=current_binding.to_dict(),
            reuse_comparison=comparison,
        )
    verified, verification_reason = _registered_source_is_verifiable(
        original,
        previous_summary,
        registry=registry,
        external_registry_resolver=external_registry_resolver,
    )
    if not verified:
        return Stage1ReuseEligibilityV1(
            decision="identity_match_unverified",
            canonical_paper_key=canonical_key,
            reason=verification_reason,
            original_source_binding=original.to_dict(),
            current_source_binding=current_binding.to_dict(),
            reuse_comparison=comparison,
        )
    return Stage1ReuseEligibilityV1(
        decision="exact_summary_reuse",
        canonical_paper_key=canonical_key,
        reason="registered_prior_binding_matches_current_source",
        original_source_binding=original.to_dict(),
        current_source_binding=current_binding.to_dict(),
        reuse_comparison=comparison,
    )


def build_binding_hash(payload: Mapping[str, Any]) -> str:
    """Return the stable hash used for a binding's derived evidence."""

    return _binding_content_hash(payload)


__all__ = [
    "STAGE1_REUSE_BINDING_VERSION",
    "STAGE1_REUSE_POLICY",
    "Stage1VisualEvidenceQualificationV1",
    "Stage1ReusableSummaryBindingV1",
    "Stage1ReusableSummaryManifestV1",
    "Stage1TypedManifestAuthorityV1",
    "Stage1ReuseEligibilityV1",
    "build_binding_hash",
    "evaluate_stage1_reuse",
    "verify_stage1_typed_manifest_authority",
]
