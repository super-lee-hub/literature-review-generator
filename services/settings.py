"""Typed settings for the current review runtime.

The settings model is intentionally strict.  It is the only place that
defines the accepted configuration surface; callers receive typed sections
from one current schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, MutableMapping

from services.repair_policy import DEFAULT_REPAIR_POLICY, parse_repair_policy


CONFIG_SCHEMA_VERSION = 3

# Kept in the accepted schema only so older config files can be read and
# normalized.  This field is not a current parser-routing control.
_DEPRECATED_PREPROCESS_KEYS = frozenset({"strategy_policy"})

API_KEYS = frozenset(
    {
        "api_key",
        "model",
        "api_base",
        "proxy_mode",
        "endpoint_type",
        "provider_family",
        "thinking",
        "reasoning_effort",
        "reasoning_display",
        "text_verbosity",
        "max_context_tokens",
        "max_output_tokens",
        "temperature",
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "total_timeout_seconds",
        "first_token_timeout_seconds",
        "transport_retries",
        "reasoning_reserve_tokens",
        "safety_margin_tokens",
        "supports_pdf_file_input",
        "pdf_file_input",
        "force_highest_reasoning",
        "omit_temperature_when_reasoning",
    }
)

CONFIG_KEYS: Dict[str, frozenset[str]] = {
    "Application": frozenset({"config_schema"}),
    "Paths": frozenset({"zotero_report", "library_path", "output_path"}),
    "Primary_Reader_API": API_KEYS,
    "Backup_Reader_API": API_KEYS,
    "Writer_API": API_KEYS,
    "Outline_API": API_KEYS,
    "Free_Mode_API": API_KEYS,
    "Validator_API": API_KEYS,
    "Runtime": frozenset(
        {
            "max_workers",
            "transport_retries",
            "node_retry_limit",
            "stage1_retry_limit",
            "review_section_retry_limit",
            "validation_retry_limit",
            "retry_base_delay_seconds",
            "retry_max_delay_seconds",
            "total_job_deadline_seconds",
            "retain_checkpoints_after_completion",
        }
    ),
    "Validation": frozenset(
        {
            "stage1_enabled",
            "review_enabled",
            "repair_policy",
            "evidence_resolver_enabled",
            "visual_refs_enabled",
            "review_drift_threshold",
            "summary_drift_threshold",
        }
    ),
    "Outline": frozenset(
        {
            "candidate_count",
            "relation_adjudication_enabled",
            "structure_critique_enabled",
            "coverage_critique_enabled",
            "evidence_critique_enabled",
            "require_explicit_adoption",
            "technical_shard_target_tokens",
            "allow_bibliometric_provider",
        }
    ),
    "OutlineModels": frozenset(
        {
            "outline_model",
            "structure_critic_model",
            "coverage_critic_model",
            "evidence_critic_model",
            "arbitrator_model",
        }
    ),
    "OutlineCostControl": frozenset(
        {
            "max_critique_models",
            "max_summary_refs_per_prompt",
            "max_outline_retry_count",
        }
    ),
    "OutlineStability": frozenset(
        {
            "mode",
            "max_provider_calls",
            "max_estimated_cost",
            "max_estimated_total_tokens",
            "pricing_source",
            "pricing_provider",
            "pricing_model",
            "pricing_version",
            "pricing_effective_date",
            "estimated_cost_per_1k_tokens",
            "input_cost_per_1k_tokens",
            "output_cost_per_1k_tokens",
            "reasoning_cost_per_1k_tokens",
            "cache_read_cost_per_1k_tokens",
            "cache_write_cost_per_1k_tokens",
            "max_smoke_overhead_ratio",
            "max_source_prompt_tokens",
        }
    ),
    "OutlineQualityGate": frozenset(
        {
            "coverage_scope",
            "min_canonical_coverage_full",
            "min_canonical_coverage_local",
            "min_effective_sections",
            "max_duplicate_assignments",
            "block_placeholder_sections",
            "block_empty_research_streams",
        }
    ),
    "Queue": frozenset({"enabled", "queue_file_path", "max_concurrent_jobs", "retry_attempts"}),
    "Preprocess": frozenset(
        {
            "enabled",
            "cache_dir",
            "strategy_policy",
            "parser_mode",
            "primary_parser",
            "fallback_parser",
            "extractor_profile",
            "ocr_mode",
            "ocr_languages",
            "force_rebuild",
            "use_markdown_as_stage1_input",
            "retain_structured_output",
            "retain_page_index",
            "retain_diagnostics",
            "enable_local_rag",
            "rag_backend",
        }
    ),
    "Styling": frozenset({"font_name", "font_size_body", "font_size_heading1", "font_size_heading2"}),
    "GUI": frozenset({"language"}),
    "Stage1_Input": frozenset(
        {
            "send_extracted_text",
            "send_selected_visuals",
            "send_original_pdf",
            "pdf_required_for_formal_precision",
            "max_pdf_file_mb",
            "formal_precision_text_only_policy",
            "force_pdf_file_input_for_provider",
            "pdf_verifier_api",
        }
    ),
    "Stage1_Visual": frozenset({"enabled", "max_visual_refs_per_paper", "visual_artifact_dir"}),
    "Multimodal": frozenset({"enabled", "multimodal_api_key", "multimodal_model", "multimodal_api_base"}),
}


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _section(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name, {})
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class ValidationSettings:
    stage1_enabled: bool = False
    review_enabled: bool = True
    repair_policy: str = DEFAULT_REPAIR_POLICY.value
    evidence_resolver_enabled: bool = True
    visual_refs_enabled: bool = True
    review_drift_threshold: float = 0.3
    summary_drift_threshold: float = 0.2

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ValidationSettings":
        section = _section(config, "Validation")
        return cls(
            stage1_enabled=_bool(section.get("stage1_enabled"), False),
            review_enabled=_bool(section.get("review_enabled"), True),
            repair_policy=parse_repair_policy(section.get("repair_policy")).value,
            evidence_resolver_enabled=_bool(section.get("evidence_resolver_enabled"), True),
            visual_refs_enabled=_bool(section.get("visual_refs_enabled"), True),
            review_drift_threshold=_float(section.get("review_drift_threshold"), 0.3),
            summary_drift_threshold=_float(section.get("summary_drift_threshold"), 0.2),
        )


@dataclass(frozen=True)
class RuntimeSettings:
    max_workers: int = 3
    transport_retries: int = 2
    node_retry_limit: int = 2
    stage1_retry_limit: int = 2
    review_section_retry_limit: int = 2
    validation_retry_limit: int = 1
    retry_base_delay_seconds: int = 30
    retry_max_delay_seconds: int = 120
    total_job_deadline_seconds: int = 0
    retain_checkpoints_after_completion: bool = False

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "RuntimeSettings":
        section = _section(config, "Runtime")
        return cls(
            max_workers=_int(section.get("max_workers"), 3),
            transport_retries=_int(section.get("transport_retries"), 2),
            node_retry_limit=_int(section.get("node_retry_limit"), 2),
            stage1_retry_limit=_int(section.get("stage1_retry_limit"), 2),
            review_section_retry_limit=_int(section.get("review_section_retry_limit"), 2),
            validation_retry_limit=_int(section.get("validation_retry_limit"), 1),
            retry_base_delay_seconds=_int(section.get("retry_base_delay_seconds"), 30),
            retry_max_delay_seconds=_int(section.get("retry_max_delay_seconds"), 120),
            total_job_deadline_seconds=_int(section.get("total_job_deadline_seconds"), 0),
            retain_checkpoints_after_completion=_bool(section.get("retain_checkpoints_after_completion"), False),
        )


@dataclass(frozen=True)
class OutlineSettings:
    candidate_count: int = 5
    relation_adjudication_enabled: bool = True
    structure_critique_enabled: bool = True
    coverage_critique_enabled: bool = True
    evidence_critique_enabled: bool = True
    require_explicit_adoption: bool = True
    technical_shard_target_tokens: int = 0
    allow_bibliometric_provider: bool = False

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "OutlineSettings":
        section = _section(config, "Outline")
        return cls(
            candidate_count=_int(section.get("candidate_count"), 5),
            relation_adjudication_enabled=_bool(section.get("relation_adjudication_enabled"), True),
            structure_critique_enabled=_bool(section.get("structure_critique_enabled"), True),
            coverage_critique_enabled=_bool(section.get("coverage_critique_enabled"), True),
            evidence_critique_enabled=_bool(section.get("evidence_critique_enabled"), True),
            require_explicit_adoption=_bool(section.get("require_explicit_adoption"), True),
            technical_shard_target_tokens=_int(section.get("technical_shard_target_tokens"), 0),
            allow_bibliometric_provider=_bool(section.get("allow_bibliometric_provider"), False),
        )


@dataclass(frozen=True)
class OutlineStabilitySettings:
    mode: str = "smoke"
    max_provider_calls: int = 24
    max_estimated_cost: float | None = None
    max_estimated_total_tokens: int = 5_000_000
    pricing_source: str = ""
    pricing_provider: str = ""
    pricing_model: str = ""
    pricing_version: str = ""
    pricing_effective_date: str = ""
    estimated_cost_per_1k_tokens: float | None = None
    input_cost_per_1k_tokens: float | None = None
    output_cost_per_1k_tokens: float | None = None
    reasoning_cost_per_1k_tokens: float | None = None
    cache_read_cost_per_1k_tokens: float | None = None
    cache_write_cost_per_1k_tokens: float | None = None
    max_smoke_overhead_ratio: float = 2.0
    max_source_prompt_tokens: int = 0

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "OutlineStabilitySettings":
        section = _section(config, "OutlineStability")
        mode = str(section.get("mode") or "smoke").strip().lower()
        if mode not in {"off", "smoke", "full"}:
            mode = "smoke"
        return cls(
            mode=mode,
            max_provider_calls=max(0, _int(section.get("max_provider_calls"), 24)),
            max_estimated_cost=_optional_float(section.get("max_estimated_cost")),
            max_estimated_total_tokens=max(
                0, _int(section.get("max_estimated_total_tokens"), 5_000_000)
            ),
            pricing_source=str(section.get("pricing_source") or "").strip(),
            pricing_provider=str(section.get("pricing_provider") or "").strip(),
            pricing_model=str(section.get("pricing_model") or "").strip(),
            pricing_version=str(section.get("pricing_version") or "").strip(),
            pricing_effective_date=str(section.get("pricing_effective_date") or "").strip(),
            estimated_cost_per_1k_tokens=_optional_float(section.get("estimated_cost_per_1k_tokens")),
            input_cost_per_1k_tokens=_optional_float(section.get("input_cost_per_1k_tokens")),
            output_cost_per_1k_tokens=_optional_float(section.get("output_cost_per_1k_tokens")),
            reasoning_cost_per_1k_tokens=_optional_float(section.get("reasoning_cost_per_1k_tokens")),
            cache_read_cost_per_1k_tokens=_optional_float(section.get("cache_read_cost_per_1k_tokens")),
            cache_write_cost_per_1k_tokens=_optional_float(section.get("cache_write_cost_per_1k_tokens")),
            max_smoke_overhead_ratio=max(1.0, _float(section.get("max_smoke_overhead_ratio"), 2.0)),
            max_source_prompt_tokens=max(0, _int(section.get("max_source_prompt_tokens"), 0)),
        )


@dataclass(frozen=True)
class ApplicationSettings:
    config_schema: int = CONFIG_SCHEMA_VERSION
    validation: ValidationSettings = field(default_factory=ValidationSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    outline: OutlineSettings = field(default_factory=OutlineSettings)
    outline_stability: OutlineStabilitySettings = field(default_factory=OutlineStabilitySettings)
    sections: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ApplicationSettings":
        application = _section(config, "Application")
        return cls(
            config_schema=_int(application.get("config_schema"), CONFIG_SCHEMA_VERSION),
            validation=ValidationSettings.from_config(config),
            runtime=RuntimeSettings.from_config(config),
            outline=OutlineSettings.from_config(config),
            outline_stability=OutlineStabilitySettings.from_config(config),
            sections={
                str(section): {str(key): str(value) for key, value in values.items()}
                for section, values in config.items()
                if isinstance(values, Mapping)
            },
        )

    @classmethod
    def from_mutable_config(cls, config: MutableMapping[str, Dict[str, str]]) -> "ApplicationSettings":
        normalized_sections = {
            str(section): {
                str(key): str(value)
                for key, value in values.items()
                if not (
                    str(section) == "Preprocess"
                    and str(key) in _DEPRECATED_PREPROCESS_KEYS
                )
            }
            for section, values in config.items()
            if isinstance(values, Mapping)
        }
        config.clear()
        config.update(normalized_sections)
        return cls.from_config(normalized_sections)

    def section(self, name: str) -> Mapping[str, str]:
        value = self.sections.get(name, {})
        return value if isinstance(value, Mapping) else {}

    def stage1_validation_enabled(self) -> bool:
        return self.validation.stage1_enabled

    def review_validation_enabled(self) -> bool:
        return self.validation.review_enabled

    def keep_checkpoints_after_completion(self) -> bool:
        return self.runtime.retain_checkpoints_after_completion

    def repair_policy(self) -> str:
        return self.validation.repair_policy

    def outline_candidate_count(self) -> int:
        return self.outline.candidate_count

    def outline_stability_settings(self) -> OutlineStabilitySettings:
        return self.outline_stability

    def outline_require_explicit_adopt(self) -> bool:
        return self.outline.require_explicit_adoption

    def outline_quality_gate_coverage_scope(self) -> str:
        value = str(self.section("OutlineQualityGate").get("coverage_scope", "full")).strip().lower()
        return value if value in {"full", "local"} else "full"

    def outline_min_canonical_coverage_full(self) -> float:
        return _float(self.section("OutlineQualityGate").get("min_canonical_coverage_full"), 0.5)

    def outline_min_canonical_coverage_local(self) -> float:
        return _float(self.section("OutlineQualityGate").get("min_canonical_coverage_local"), 0.25)

    def outline_min_effective_sections(self) -> int:
        return _int(self.section("OutlineQualityGate").get("min_effective_sections"), 3)

    def outline_max_duplicate_assignments(self) -> int:
        return _int(self.section("OutlineQualityGate").get("max_duplicate_assignments"), 0)

    def outline_block_placeholder_sections(self) -> bool:
        return _bool(self.section("OutlineQualityGate").get("block_placeholder_sections"), True)

    def outline_block_empty_research_streams(self) -> bool:
        return _bool(self.section("OutlineQualityGate").get("block_empty_research_streams"), True)

    def outline_quality_gate(self) -> Any:
        """Return the complete typed Outline v3 quality gate.

        The import is intentionally local: ``outline.v3_models`` also owns
        model-facing hashes and must not become an import-time dependency of
        the settings loader.
        """

        from outline.v3_models import OutlineQualityGate

        return OutlineQualityGate(
            coverage_scope=self.outline_quality_gate_coverage_scope(),
            min_canonical_coverage_full=self.outline_min_canonical_coverage_full(),
            min_canonical_coverage_local=self.outline_min_canonical_coverage_local(),
            min_effective_sections=self.outline_min_effective_sections(),
            max_duplicate_assignments=self.outline_max_duplicate_assignments(),
            block_placeholder_sections=self.outline_block_placeholder_sections(),
            block_empty_research_streams=self.outline_block_empty_research_streams(),
        )

    def outline_model(self) -> str:
        return str(self.section("OutlineModels").get("outline_model", "Outline_API")).strip()

    def structure_critic_model(self) -> str:
        return str(self.section("OutlineModels").get("structure_critic_model", "Writer_API")).strip()

    def coverage_critic_model(self) -> str:
        return str(self.section("OutlineModels").get("coverage_critic_model", "Primary_Reader_API")).strip()

    def evidence_critic_model(self) -> str:
        return str(self.section("OutlineModels").get("evidence_critic_model", "Primary_Reader_API")).strip()

    def arbitrator_model(self) -> str:
        return str(self.section("OutlineModels").get("arbitrator_model", "Outline_API")).strip()

    def outline_max_critique_models(self) -> int:
        return _int(self.section("OutlineCostControl").get("max_critique_models"), 2)

    def outline_max_summary_refs_per_prompt(self) -> int:
        return _int(self.section("OutlineCostControl").get("max_summary_refs_per_prompt"), 80)

    def outline_max_retry_count(self) -> int:
        return _int(self.section("OutlineCostControl").get("max_outline_retry_count"), 2)

    def validate_outline_config(self) -> list[str]:
        errors: list[str] = []
        count = self.outline_candidate_count()
        if count < 1:
            errors.append("Outline.candidate_count must be at least 1")
        if count > 12:
            errors.append("Outline.candidate_count must not exceed 12")
        if not self.outline_model():
            errors.append("OutlineModels.outline_model is not configured")
        if not self.structure_critic_model():
            errors.append("OutlineModels.structure_critic_model is not configured")
        if not self.coverage_critic_model():
            errors.append("OutlineModels.coverage_critic_model is not configured")
        if not self.evidence_critic_model():
            errors.append("OutlineModels.evidence_critic_model is not configured")
        if not self.arbitrator_model():
            errors.append("OutlineModels.arbitrator_model is not configured")
        return errors


def validate_config_keys(config: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Return strict unknown-section/key errors for the current schema."""

    legacy_section_replacements = {
        "Retry_Settings": "Use [Runtime].review_section_retry_limit and the other typed [Runtime] retry keys instead.",
        "Stage2_Retry": "Use the typed retry keys in [Runtime] instead.",
        "API_Parameters": "Put provider-owned limits and timeouts directly in the relevant provider section.",
    }
    legacy_key_replacements = {
        ("Outline", "test_dev_fixture_mode"): (
            "Fixture providers are test-injected only and cannot be enabled by production configuration."
        ),
        ("Runtime", "max_retry_rounds"): "Use [Runtime].node_retry_limit instead.",
        ("Runtime", "base_retry_delay"): "Use [Runtime].retry_base_delay_seconds instead.",
        ("Runtime", "max_retry_delay"): "Use [Runtime].retry_max_delay_seconds instead.",
    }
    errors: list[str] = []
    for section_name, values in config.items():
        if section_name in legacy_section_replacements:
            errors.append(
                f"Unsupported current configuration section [{section_name}]. "
                f"{legacy_section_replacements[section_name]}"
            )
            continue
        allowed = CONFIG_KEYS.get(section_name)
        if allowed is None:
            for key in values:
                errors.append(
                    f"Unsupported configuration key: [{section_name}].{key}. "
                    "This project accepts only the current typed configuration sections."
                )
            if not values:
                errors.append(f"Unsupported configuration section: [{section_name}]")
            continue
        for key in values:
            if str(key) not in allowed:
                replacement = legacy_key_replacements.get((section_name, str(key)))
                suffix = f" {replacement}" if replacement else ""
                errors.append(
                    f"Unsupported configuration key: [{section_name}].{key}. "
                    "This project accepts only the current typed configuration keys."
                    f"{suffix}"
                )
    return errors


__all__ = [
    "API_KEYS",
    "CONFIG_KEYS",
    "CONFIG_SCHEMA_VERSION",
    "ApplicationSettings",
    "ValidationSettings",
    "RuntimeSettings",
    "OutlineSettings",
    "validate_config_keys",
]
