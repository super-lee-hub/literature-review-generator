from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, MutableMapping

from services.repair_policy import DEFAULT_REPAIR_POLICY, parse_repair_policy


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ValidationCompatSettings:
    stage1_enabled: bool = False
    stage2_enabled: bool = False
    keep_checkpoints_after_completion: bool = False
    repair_policy: str = DEFAULT_REPAIR_POLICY.value

    def to_section(self) -> Dict[str, str]:
        return {
            "stage1_enabled": "true" if self.stage1_enabled else "false",
            "stage2_enabled": "true" if self.stage2_enabled else "false",
            "keep_checkpoints_after_completion": "true" if self.keep_checkpoints_after_completion else "false",
            "repair_policy": self.repair_policy,
        }


def read_validation_settings(config: Mapping[str, Any] | None) -> ValidationCompatSettings:
    validation_section = dict(config.get("Validation", {})) if config else {}
    performance_section = dict(config.get("Performance", {})) if config else {}

    return ValidationCompatSettings(
        stage1_enabled=_as_bool(
            validation_section.get("stage1_enabled"),
            default=_as_bool(performance_section.get("enable_stage1_validation"), default=False),
        ),
        stage2_enabled=_as_bool(
            validation_section.get("stage2_enabled"),
            default=_as_bool(performance_section.get("enable_stage2_validation"), default=False),
        ),
        keep_checkpoints_after_completion=_as_bool(
            validation_section.get("keep_checkpoints_after_completion"),
            default=_as_bool(
                performance_section.get("keep_checkpoints_after_completion"),
                default=_as_bool(config.get("keep_checkpoints_after_completion"), default=False) if config else False,
            ),
        ),
        repair_policy=parse_repair_policy(validation_section.get("repair_policy")).value,
    )


def apply_validation_compat_sections(
    sections: MutableMapping[str, Dict[str, str]] | Mapping[str, Mapping[str, str]] | None,
) -> Dict[str, Dict[str, str]]:
    normalized: Dict[str, Dict[str, str]] = {}
    for section_name, values in (sections or {}).items():
        normalized[section_name] = {str(key): str(value) for key, value in values.items()}

    normalized.setdefault("Performance", {})
    settings = read_validation_settings(normalized)
    validation_section = dict(normalized.get("Validation", {}))
    validation_section.update(settings.to_section())
    normalized["Validation"] = validation_section
    normalized["Performance"]["enable_stage1_validation"] = "true" if settings.stage1_enabled else "false"
    normalized["Performance"]["enable_stage2_validation"] = "true" if settings.stage2_enabled else "false"
    return normalized


def _outline_bool(view: "CompatConfigView", key: str, default: bool = False) -> bool:
    outline = view.raw_config.get("Outline", {})
    return _as_bool(outline.get(key), default=default)


def _outline_int(view: "CompatConfigView", key: str, default: int = 0) -> int:
    outline = view.raw_config.get("Outline", {})
    try:
        return int(outline.get(key, default))
    except (ValueError, TypeError):
        return default


def _outline_int_parse_error(view: "CompatConfigView", key: str) -> str:
    """Return a validation message when an explicit integer value is malformed."""
    outline = view.raw_config.get("Outline", {})
    if key not in outline:
        return ""
    value = outline.get(key)
    try:
        int(value)
    except (ValueError, TypeError):
        return f"{key}={value!r} must be an integer"
    return ""


def _cost_int_parse_error(view: "CompatConfigView", key: str) -> str:
    cost = view.raw_config.get("OutlineCostControl", {})
    if key not in cost:
        return ""
    value = cost.get(key)
    try:
        int(value)
    except (ValueError, TypeError):
        return f"{key}={value!r} must be an integer"
    return ""


def _quality_gate_section(view: "CompatConfigView") -> Dict[str, Any]:
    return dict(view.raw_config.get("OutlineQualityGate", {}))


def _quality_gate_bool(view: "CompatConfigView", key: str, default: bool = False) -> bool:
    return _as_bool(_quality_gate_section(view).get(key), default=default)


def _quality_gate_int(view: "CompatConfigView", key: str, default: int = 0) -> int:
    try:
        return int(_quality_gate_section(view).get(key, default))
    except (ValueError, TypeError):
        return default


def _quality_gate_float(view: "CompatConfigView", key: str, default: float = 0.0) -> float:
    try:
        return float(_quality_gate_section(view).get(key, default))
    except (ValueError, TypeError):
        return default


def _outline_model_str(view: "CompatConfigView", key: str, default: str = "") -> str:
    models = view.raw_config.get("OutlineModels", {})
    return str(models.get(key, default)).strip()


@dataclass
class CompatConfigView:
    raw_config: MutableMapping[str, Dict[str, str]]
    validation: ValidationCompatSettings

    @classmethod
    def from_config(cls, config: MutableMapping[str, Dict[str, str]]) -> "CompatConfigView":
        normalized = apply_validation_compat_sections(config)
        config.clear()
        config.update(normalized)
        return cls(raw_config=config, validation=read_validation_settings(config))

    # -- Validation compat --

    def stage1_validation_enabled(self) -> bool:
        return self.validation.stage1_enabled

    def stage2_validation_enabled(self) -> bool:
        return self.validation.stage2_enabled

    def keep_checkpoints_after_completion(self) -> bool:
        return self.validation.keep_checkpoints_after_completion

    def repair_policy(self) -> str:
        return self.validation.repair_policy

    # -- Outline v2 config --

    def outline_v2_enabled(self) -> bool:
        return _outline_bool(self, "enable_outline_intelligence_v2", default=False)

    def outline_literature_map_enabled(self) -> bool:
        return _outline_bool(self, "enable_literature_map", default=True)

    def outline_synthesis_flow_enabled(self) -> bool:
        return _outline_bool(self, "enable_synthesis_flow", default=True)

    def outline_candidate_count(self) -> int:
        return _outline_int(self, "candidate_count", default=3)

    def outline_max_candidate_count(self) -> int:
        cost = self.raw_config.get("OutlineCostControl", {})
        try:
            return int(cost.get("max_candidate_count", 3))
        except (ValueError, TypeError):
            return 3

    def outline_multi_model_critique_enabled(self) -> bool:
        return _outline_bool(self, "enable_multi_model_critique", default=True)

    def outline_coverage_audit_enabled(self) -> bool:
        return _outline_bool(self, "enable_coverage_audit", default=True)

    def outline_require_explicit_adopt(self) -> bool:
        return _outline_bool(self, "require_explicit_adopt", default=True)

    def outline_allow_bibliometric_provider(self) -> bool:
        return _outline_bool(self, "allow_bibliometric_provider", default=False)

    def outline_test_dev_fixture_mode(self) -> bool:
        return _outline_bool(self, "test_dev_fixture_mode", default=False)

    # -- Outline quality gate --

    def outline_quality_gate_coverage_scope(self) -> str:
        scope = str(_quality_gate_section(self).get("coverage_scope", "full")).strip().lower()
        return scope if scope in {"full", "local"} else "full"

    def outline_min_canonical_coverage_full(self) -> float:
        return _quality_gate_float(self, "min_canonical_coverage_full", default=0.50)

    def outline_min_canonical_coverage_local(self) -> float:
        return _quality_gate_float(self, "min_canonical_coverage_local", default=0.25)

    def outline_min_effective_sections(self) -> int:
        return _quality_gate_int(self, "min_effective_sections", default=3)

    def outline_max_duplicate_assignments(self) -> int:
        return _quality_gate_int(self, "max_duplicate_assignments", default=0)

    def outline_block_placeholder_sections(self) -> bool:
        return _quality_gate_bool(self, "block_placeholder_sections", default=True)

    def outline_block_empty_research_streams(self) -> bool:
        return _quality_gate_bool(self, "block_empty_research_streams", default=True)

    # -- Outline model routing --

    def outline_model(self) -> str:
        return _outline_model_str(self, "outline_model", default="Outline_API")

    def structure_critic_model(self) -> str:
        return _outline_model_str(self, "structure_critic_model", default="Writer_API")

    def coverage_critic_model(self) -> str:
        return _outline_model_str(self, "coverage_critic_model", default="Primary_Reader_API")

    def arbitrator_model(self) -> str:
        return _outline_model_str(self, "arbitrator_model", default="Outline_API")

    # -- Cost control --

    def outline_max_critique_models(self) -> int:
        cost = self.raw_config.get("OutlineCostControl", {})
        try:
            return int(cost.get("max_critique_models", 2))
        except (ValueError, TypeError):
            return 2

    def outline_max_summary_refs_per_prompt(self) -> int:
        cost = self.raw_config.get("OutlineCostControl", {})
        try:
            return int(cost.get("max_summary_refs_per_prompt", 80))
        except (ValueError, TypeError):
            return 80

    def outline_max_retry_count(self) -> int:
        cost = self.raw_config.get("OutlineCostControl", {})
        try:
            return int(cost.get("max_outline_retry_count", 2))
        except (ValueError, TypeError):
            return 2

    # -- V2 config validation --

    def validate_outline_v2_config(self) -> list[str]:
        """Validate v2 config. Returns list of error strings."""
        errors: list[str] = []
        if not self.outline_v2_enabled():
            return errors  # No v2 validation when v2 is disabled

        candidate_parse_error = _outline_int_parse_error(self, "candidate_count")
        if candidate_parse_error:
            errors.append(candidate_parse_error)

        max_candidate_parse_error = _cost_int_parse_error(self, "max_candidate_count")
        if max_candidate_parse_error:
            errors.append(max_candidate_parse_error)

        count = self.outline_candidate_count()
        if not candidate_parse_error:
            if count < 2:
                errors.append(f"candidate_count={count} is below minimum of 2 for production v2")
            if count > 3:
                errors.append(f"candidate_count={count} exceeds maximum of 3 for production v2")

        max_count = self.outline_max_candidate_count()
        if not max_candidate_parse_error and max_count != 3:
            errors.append(f"max_candidate_count={max_count} must be 3")

        if not self.outline_model():
            errors.append("outline_model is not configured")
        if not self.structure_critic_model():
            errors.append("structure_critic_model is not configured")
        if not self.coverage_critic_model():
            errors.append("coverage_critic_model is not configured")

        return errors

