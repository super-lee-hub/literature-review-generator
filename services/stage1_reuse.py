"""Typed eligibility and provenance for Stage 1 summary reuse."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from services.artifact_registry import ArtifactRegistry, file_sha256
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
    "prompt_template_hash",
    "input_builder_policy_hash",
    "provider",
    "model",
    "endpoint_type",
    "provider_config_hash",
    "summary_schema_hash",
    "visual_input_manifest_hash",
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
    prompt_template_hash: str = ""
    input_builder_policy_hash: str = ""
    summary_schema_hash: str = ""
    visual_input_manifest_hash: str = ""
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
        known: dict[str, Any] = {
            name: (
                bool(raw[name])
                if name == "location_changed"
                else _text(raw[name])
            )
            for name in cls.__dataclass_fields__
            if name != "extra" and name in raw and raw[name] is not None
        }
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
                "prompt_template_hash",
                "input_builder_policy_hash",
                "summary_schema_hash",
                "visual_input_manifest_hash",
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
            if not original and not actual and field_name in _OPTIONAL_COMPARISON_FIELDS:
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
    summary_payload_hash: str = ""
    binding_hash: str = ""
    source_pdf_content_sha256: str = ""
    stage1_extracted_text_hash: str = ""
    stage1_semantic_input_hash: str = ""
    preprocess_contract_hash: str = ""
    prompt_template_hash: str = ""
    input_builder_policy_hash: str = ""
    summary_schema_hash: str = ""
    visual_input_manifest_hash: str = ""
    provider_receipt_closure_id: str = ""
    provider_receipt_closure_hash: str = ""
    provider_receipt_ledger_id: str = ""
    provider_receipt_ledger_hash: str = ""
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
        known = {
            name: _text(raw[name])
            for name in cls.__dataclass_fields__
            if name in raw
        }
        return cls(**known)


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
        authoritative_summary = matching.get("analysis")
    if not isinstance(authoritative_summary, Mapping):
        return False, "registered_source_artifact_payload_summary_missing"
    authoritative_hash = hash_json(authoritative_summary)
    imported_hash = hash_json(imported_summary)
    if authoritative_hash != imported_hash:
        return False, "registered_source_artifact_payload_mismatch"
    declared_hash = str(
        matching.get("summary_payload_hash")
        or matching.get("ai_summary_hash")
        or ""
    ).strip()
    if declared_hash and declared_hash != authoritative_hash:
        return False, "registered_source_artifact_payload_hash_mismatch"
    bound_hash = _text(binding.summary_payload_hash)
    if bound_hash and bound_hash != authoritative_hash:
        return False, "registered_source_artifact_summary_payload_hash_mismatch"
    return True, "registered_source_artifact_payload_verified"


def _registered_source_is_verifiable(
    binding: Stage1ReusableSummaryBindingV1,
    previous_summary: Mapping[str, Any],
    *,
    registry: ArtifactRegistry | None,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> tuple[bool, str]:
    authority_id = _text(binding.source_authority_artifact_id) or _text(
        binding.registered_source_artifact_id
    )
    if not authority_id:
        return False, "registered_source_artifact_id_missing"
    authority_job_id = _text(binding.source_authority_job_id)
    target_registry = registry
    if authority_job_id and registry is not None and authority_job_id != registry.job_id:
        if external_registry_resolver is not None:
            target_registry = external_registry_resolver(authority_job_id)
        elif _text(binding.source_authority_registry_path):
            try:
                target_registry = ArtifactRegistry(
                    binding.source_authority_registry_path,
                    authority_job_id,
                )
            except (OSError, TypeError, ValueError, RuntimeError):
                target_registry = None
        else:
            return False, "source_authority_registry_resolver_missing"
        if target_registry is None:
            return False, "source_authority_registry_unavailable"
        target_registry.reload()
    if target_registry is None:
        return False, "source_authority_registry_missing"
    record = target_registry.get(authority_id)
    if record is None:
        return False, "registered_source_artifact_not_registered"
    if record.status != "ready":
        return False, "registered_source_artifact_not_ready"
    if authority_job_id and record.job_id != authority_job_id:
        return False, "source_authority_job_mismatch"
    expected_content_hash = _text(binding.source_authority_artifact_hash) or _text(
        binding.registered_source_artifact_hash
    )
    expected_file_hash = _text(binding.registry_file_hash)
    if expected_content_hash and record.content_hash != expected_content_hash:
        return False, "registered_source_artifact_hash_mismatch"
    path = record.path
    declared_path = _text(binding.source_authority_artifact_path) or _text(
        binding.registered_source_artifact_path
    )
    if declared_path and Path(declared_path).resolve() != Path(path).resolve():
        return False, "registered_source_artifact_path_mismatch"
    try:
        ArtifactRegistry._verify_ready_artifact(record)
        if record.depends_on:
            target_registry.verify_ready_dependencies(
                record.depends_on,
                external_registry_resolver=external_registry_resolver,
            )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return False, f"registered_source_artifact_untrusted:{exc}"
    actual_hash = _hash_file(path)
    if not actual_hash:
        return False, "registered_source_artifact_missing"
    if expected_file_hash and actual_hash != expected_file_hash:
        return False, "registered_source_artifact_file_hash_mismatch"
    if actual_hash != record.content_hash:
        return False, "registered_source_artifact_content_hash_mismatch"
    payload_ok, payload_reason = _authority_summary_matches(
        path=path,
        canonical_paper_key=_text(
            _mapping(previous_summary.get("paper_info")).get("canonical_paper_key")
            or binding.canonical_paper_key
        ),
        previous_summary=previous_summary,
        binding=binding,
    )
    if not payload_ok:
        return False, payload_reason
    return True, "registered_source_artifact_verified"


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

    return hash_json(dict(payload))


__all__ = [
    "STAGE1_REUSE_BINDING_VERSION",
    "STAGE1_REUSE_POLICY",
    "Stage1ReusableSummaryBindingV1",
    "Stage1ReusableSummaryManifestV1",
    "Stage1ReuseEligibilityV1",
    "build_binding_hash",
    "evaluate_stage1_reuse",
]
