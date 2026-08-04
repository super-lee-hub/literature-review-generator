"""One typed stage policy shared by the runner, lifecycle, and closure readers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping


class StagePlanError(ValueError):
    """Raised when a requested stage policy cannot be executed safely."""


_DEFAULTS: dict[str, tuple[str, ...]] = {
    "analyze": ("analyze",),
    "derive_review_batch": ("derive_review_batch",),
    "retry_failed": ("analyze",),
    "generate_outline": ("outline",),
    "generate_review": ("outline", "review"),
    "generate_section": ("outline", "review"),
    "retry_review_failed": ("outline", "review"),
    "validate_review": ("validate",),
    "run_all": ("analyze", "outline", "review"),
}

_CURRENT_SET_ACTIONS = frozenset(
    {
        "derive_review_batch",
        "generate_outline",
        "generate_review",
        "generate_section",
        "retry_review_failed",
        "run_all",
        "validate_review",
    }
)


@dataclass(frozen=True)
class StagePlan:
    version: str
    action: str
    requested_stages: tuple[str, ...]
    required_stages: tuple[str, ...]
    validation_enabled: bool
    validation_required: bool
    require_clean_validation: bool
    allow_unvalidated_when_validation_optional: bool
    current_artifact_set_required: bool
    validation_status: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requested_stages"] = list(self.requested_stages)
        payload["required_stages"] = list(self.required_stages)
        return payload


def _normalize(raw: Iterable[Any] | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(
        dict.fromkeys(
            str(item).strip()
            for item in raw
            if str(item).strip() and str(item).strip() != "source_intake"
        )
    )


def build_stage_plan(
    *,
    action: str,
    requested_stages: Iterable[Any] | None,
    validation_enabled: bool,
    validation_required: bool | None = None,
    require_clean_validation: bool | None = None,
    allow_unvalidated_when_validation_optional: bool | None = None,
) -> StagePlan:
    normalized_action = str(action or "analyze")
    explicit = _normalize(requested_stages)
    default_stages = _DEFAULTS.get(normalized_action, ())
    if explicit is None:
        stages = tuple(default_stages)
        if normalized_action == "run_all" and validation_enabled:
            stages = (*stages, "validate")
    else:
        stages = explicit

    configured_required = validation_required
    if configured_required is None:
        configured_required = "validate" in stages or normalized_action == "validate_review"
    required = bool(configured_required)
    if normalized_action == "validate_review" and not validation_enabled and not required:
        stages = tuple(stage for stage in stages if stage != "validate")
    if "validate" in stages and not validation_enabled:
        if required:
            raise StagePlanError(
                "validation is required by the durable stage plan but Validation.review_enabled is false"
            )
        stages = tuple(stage for stage in stages if stage != "validate")

    # A required validation stage must remain in the plan; silently dropping it
    # would turn a provider-free run into a false completion.
    if required and "validate" not in stages:
        raise StagePlanError("validation_required=true but the stage plan has no validate stage")

    clean = required if require_clean_validation is None else bool(require_clean_validation)
    allow = (not required) if allow_unvalidated_when_validation_optional is None else bool(
        allow_unvalidated_when_validation_optional
    )
    required_stages = ("source_intake", *stages)
    return StagePlan(
        version="stage-plan-v1",
        action=normalized_action,
        requested_stages=tuple(stages),
        required_stages=tuple(dict.fromkeys(required_stages)),
        validation_enabled=bool(validation_enabled),
        validation_required=required,
        require_clean_validation=clean,
        allow_unvalidated_when_validation_optional=allow,
        current_artifact_set_required=(
            "validate" in stages or normalized_action in _CURRENT_SET_ACTIONS
        ),
        validation_status="required" if "validate" in stages else "not_requested",
    )


def stage_plan_from_metadata(metadata: Mapping[str, Any]) -> StagePlan | None:
    raw = metadata.get("stage_plan")
    if not isinstance(raw, Mapping):
        return None
    try:
        requested = tuple(str(item) for item in raw.get("requested_stages") or ())
        required = tuple(str(item) for item in raw.get("required_stages") or ())
        return StagePlan(
            version=str(raw.get("version") or "stage-plan-v1"),
            action=str(raw.get("action") or ""),
            requested_stages=requested,
            required_stages=required,
            validation_enabled=bool(raw.get("validation_enabled")),
            validation_required=bool(raw.get("validation_required")),
            require_clean_validation=bool(raw.get("require_clean_validation")),
            allow_unvalidated_when_validation_optional=bool(
                raw.get("allow_unvalidated_when_validation_optional")
            ),
            current_artifact_set_required=bool(raw.get("current_artifact_set_required")),
            validation_status=str(raw.get("validation_status") or "not_requested"),
        )
    except (TypeError, ValueError):
        return None
