"""Typed eligibility and provenance for Stage 1 summary reuse."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping

from services.artifact_registry import ArtifactRegistry, file_sha256
from runtime.provider_runtime import hash_json


STAGE1_REUSE_BINDING_VERSION = "v1"
STAGE1_REUSE_POLICY = "exact_summary_reuse_v1"

_COMPARISON_FIELDS = (
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

# Provider names are optional in the application configuration.  An omitted
# provider is still a stable binding when both the prior and current runs omit
# it; a value appearing or changing remains a binding change.
_OPTIONAL_COMPARISON_FIELDS = frozenset({"provider"})


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
            name: _text(raw[name])
            for name in cls.__dataclass_fields__
            if name != "extra" and name in raw and raw[name] is not None
        }
        aliases = {
            "source_pdf_hash": ("source_pdf_content_hash", "pdf_content_hash"),
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
        known["extra"] = dict(raw.get("extra") or {}) if isinstance(raw.get("extra"), Mapping) else {}
        return cls(**known)

    def comparison_projection(self) -> dict[str, str]:
        return {field_name: _text(getattr(self, field_name)) for field_name in _COMPARISON_FIELDS}

    def compare(self, current: "Stage1ReusableSummaryBindingV1") -> dict[str, Any]:
        mismatches: dict[str, dict[str, str]] = {}
        missing: list[str] = []
        for field_name in _COMPARISON_FIELDS:
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
            "compared_fields": list(_COMPARISON_FIELDS),
            "missing_fields": missing,
            "mismatches": mismatches,
            "original": self.comparison_projection(),
            "current": current.comparison_projection(),
        }


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


def _registered_source_is_verifiable(
    binding: Stage1ReusableSummaryBindingV1,
    *,
    registry: ArtifactRegistry | None,
) -> tuple[bool, str]:
    record = registry.get(binding.registered_source_artifact_id) if registry and binding.registered_source_artifact_id else None
    path = _text(binding.registered_source_artifact_path)
    expected_content_hash = _text(binding.registered_source_artifact_hash)
    expected_file_hash = _text(binding.registry_file_hash)
    if record is not None:
        if record.status != "ready":
            return False, "registered_source_artifact_not_ready"
        path = record.path
        expected_content_hash = expected_content_hash or record.content_hash
        expected_file_hash = expected_file_hash or record.content_hash
        if record.content_hash != expected_content_hash:
            return False, "registered_source_artifact_hash_mismatch"
    if not path:
        return False, "registered_source_artifact_path_missing"
    actual_hash = _hash_file(path)
    if not actual_hash:
        return False, "registered_source_artifact_missing"
    if expected_file_hash and actual_hash != expected_file_hash:
        return False, "registered_source_artifact_file_hash_mismatch"
    if expected_content_hash and actual_hash != expected_content_hash:
        return False, "registered_source_artifact_content_hash_mismatch"
    return True, "registered_source_artifact_verified"


def evaluate_stage1_reuse(
    previous_summary: Mapping[str, Any],
    current_binding: Stage1ReusableSummaryBindingV1,
    *,
    registry: ArtifactRegistry | None = None,
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
    verified, verification_reason = _registered_source_is_verifiable(original, registry=registry)
    if not verified:
        return Stage1ReuseEligibilityV1(
            decision="identity_match_unverified",
            canonical_paper_key=canonical_key,
            reason=verification_reason,
            original_source_binding=original.to_dict(),
            current_source_binding=current_binding.to_dict(),
            reuse_comparison=comparison,
        )
    if not comparison["equal"]:
        source_changed = any(
            field_name in comparison.get("mismatches", {})
            for field_name in ("source_pdf_hash", "source_pdf_fingerprint", "preprocess_hash")
        )
        return Stage1ReuseEligibilityV1(
            decision="identity_match_but_stale" if source_changed else "binding_mismatch",
            canonical_paper_key=canonical_key,
            reason="registered_prior_binding_does_not_match_current_source",
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
    "Stage1ReuseEligibilityV1",
    "build_binding_hash",
    "evaluate_stage1_reuse",
]
