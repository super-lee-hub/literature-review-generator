from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, MutableMapping


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

    def to_section(self) -> Dict[str, str]:
        return {
            "stage1_enabled": "true" if self.stage1_enabled else "false",
            "stage2_enabled": "true" if self.stage2_enabled else "false",
            "keep_checkpoints_after_completion": "true" if self.keep_checkpoints_after_completion else "false",
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

    def stage1_validation_enabled(self) -> bool:
        return self.validation.stage1_enabled

    def stage2_validation_enabled(self) -> bool:
        return self.validation.stage2_enabled

    def keep_checkpoints_after_completion(self) -> bool:
        return self.validation.keep_checkpoints_after_completion

