"""Canonical strict parsers for current configuration values.

The configuration file is text, but current runtime controls must not turn a
misspelled value into a different policy.  These helpers are intentionally
small and are shared by validation, settings normalization, and Stage 1
runtime owners.
"""

from __future__ import annotations

import math
from typing import Any, Mapping


class StrictConfigValueError(ValueError):
    """Raised when a current configuration value is not in its contract."""


_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def _field_name(field: str) -> str:
    return str(field or "configuration value").strip() or "configuration value"


def parse_strict_bool(
    value: Any,
    *,
    field: str,
    default: bool | None = None,
) -> bool:
    """Parse one current boolean without unknown-value coercion."""

    name = _field_name(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return bool(default)
        raise StrictConfigValueError(f"[{name}] must be a boolean value")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    normalized = str(value).strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    accepted = "true/false/1/0/yes/no/on/off"
    raise StrictConfigValueError(
        f"[{name}] must be one of {accepted}; got {str(value)!r}"
    )


def parse_enum(
    value: Any,
    *,
    field: str,
    allowed: Mapping[str, str] | set[str] | frozenset[str] | tuple[str, ...] | list[str],
    default: str | None = None,
) -> str:
    """Parse a case-insensitive enum and return its canonical value."""

    name = _field_name(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return str(default)
        raise StrictConfigValueError(f"[{name}] must be one of {sorted(str(item) for item in allowed)}")
    normalized_allowed = {
        str(key).strip().casefold(): str(value)
        for key, value in (allowed.items() if isinstance(allowed, Mapping) else ((item, item) for item in allowed))
    }
    normalized = str(value).strip().casefold()
    if normalized in normalized_allowed:
        return normalized_allowed[normalized]
    raise StrictConfigValueError(
        f"[{name}] must be one of {sorted(normalized_allowed)}; got {str(value)!r}"
    )


def parse_bounded_float(
    value: Any,
    *,
    field: str,
    minimum: float,
    maximum: float,
    default: float | None = None,
) -> float:
    """Parse a finite float within an inclusive, explicit interval."""

    name = _field_name(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return float(default)
        raise StrictConfigValueError(f"[{name}] must be a finite number")
    if isinstance(value, bool):
        raise StrictConfigValueError(f"[{name}] must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StrictConfigValueError(f"[{name}] must be a finite number; got {str(value)!r}") from exc
    if not math.isfinite(parsed) or parsed < float(minimum) or parsed > float(maximum):
        raise StrictConfigValueError(
            f"[{name}] must be between {float(minimum):g} and {float(maximum):g}; got {str(value)!r}"
        )
    return parsed


_STAGE1_INPUT_BOOL_KEYS = frozenset(
    {
        "send_extracted_text",
        "send_selected_visuals",
        "force_pdf_file_input_for_provider",
        "require_complete_visual_coverage",
    }
)
_STAGE1_VISUAL_BOOL_KEYS = frozenset(
    {"enabled", "render_all_nonblank_pages", "table_crop_enabled", "formula_crop_enabled"}
)


def normalize_stage1_config_sections(
    sections: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """Return a copy with current Stage 1 values canonically normalized.

    Missing keys remain missing so callers can retain their existing default
    layering.  Present keys are validated before they are normalized.
    """

    normalized: dict[str, dict[str, str]] = {
        str(section): {str(key): str(value) for key, value in values.items()}
        for section, values in sections.items()
        if isinstance(values, Mapping)
    }
    stage1_input = normalized.get("Stage1_Input")
    if stage1_input is not None:
        for key in _STAGE1_INPUT_BOOL_KEYS:
            if key in stage1_input:
                stage1_input[key] = "true" if parse_strict_bool(
                    stage1_input[key], field=f"Stage1_Input.{key}"
                ) else "false"
        if "send_original_pdf" in stage1_input:
            stage1_input["send_original_pdf"] = parse_enum(
                stage1_input["send_original_pdf"],
                field="Stage1_Input.send_original_pdf",
                allowed=("never", "auto", "always"),
            )
        if "mode" in stage1_input:
            stage1_input["mode"] = parse_enum(
                stage1_input["mode"],
                field="Stage1_Input.mode",
                allowed=("vision_first",),
            )
        if "image_transport" in stage1_input:
            stage1_input["image_transport"] = parse_enum(
                stage1_input["image_transport"],
                field="Stage1_Input.image_transport",
                allowed=("base64",),
            )

    stage1_visual = normalized.get("Stage1_Visual")
    if stage1_visual is not None:
        for key in _STAGE1_VISUAL_BOOL_KEYS:
            if key in stage1_visual:
                stage1_visual[key] = "true" if parse_strict_bool(
                    stage1_visual[key], field=f"Stage1_Visual.{key}"
                ) else "false"
        if "crop_padding_ratio" in stage1_visual:
            padding = parse_bounded_float(
                stage1_visual["crop_padding_ratio"],
                field="Stage1_Visual.crop_padding_ratio",
                minimum=0.0,
                maximum=0.25,
            )
            stage1_visual["crop_padding_ratio"] = format(padding, ".15g")

    return normalized


__all__ = [
    "StrictConfigValueError",
    "normalize_stage1_config_sections",
    "parse_bounded_float",
    "parse_enum",
    "parse_strict_bool",
]
