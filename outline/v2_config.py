"""V2 configuration reader and validator for Outline Intelligence v2.

Centralizes v2 flag reading, candidate-count bounds checking, and test/dev
fixture mode detection so main.py and v2 modules don't duplicate logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional


# ---------------------------------------------------------------------------
# Default values (match executable spec)
# ---------------------------------------------------------------------------

OUTLINE_DEFAULTS: Dict[str, str] = {
    "enable_outline_intelligence_v2": "false",
    "enable_literature_map": "true",
    "enable_synthesis_flow": "true",
    "candidate_count": "3",
    "enable_multi_model_critique": "true",
    "enable_coverage_audit": "true",
    "require_explicit_adopt": "true",
    "allow_bibliometric_provider": "false",
}

OUTLINE_MODEL_DEFAULTS: Dict[str, str] = {
    "outline_model": "Outline_API",
    "structure_critic_model": "Writer_API",
    "coverage_critic_model": "Primary_Reader_API",
    "arbitrator_model": "Outline_API",
}

OUTLINE_COST_CONTROL_DEFAULTS: Dict[str, str] = {
    "max_candidate_count": "3",
    "max_critique_models": "2",
    "max_summary_refs_per_prompt": "80",
    "max_outline_retry_count": "2",
}

OUTLINE_QUALITY_GATE_DEFAULTS: Dict[str, str] = {
    "coverage_scope": "full",
    "min_canonical_coverage_full": "0.50",
    "min_canonical_coverage_local": "0.25",
    "min_effective_sections": "3",
    "max_duplicate_assignments": "0",
    "block_placeholder_sections": "true",
    "block_empty_research_streams": "true",
}

PRODUCTION_MIN_CANDIDATE_COUNT = 2
PRODUCTION_MAX_CANDIDATE_COUNT = 3


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class OutlineQualityGateConfig:
    """Parsed quality-gate policy for v2 coverage/adoption."""

    coverage_scope: str = "full"
    min_canonical_coverage_full: float = 0.50
    min_canonical_coverage_local: float = 0.25
    min_effective_sections: int = 3
    max_duplicate_assignments: int = 0
    block_placeholder_sections: bool = True
    block_empty_research_streams: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "OutlineQualityGateConfig":
        section = dict(config.get("OutlineQualityGate", {})) if config else {}
        scope = str(
            section.get("coverage_scope", OUTLINE_QUALITY_GATE_DEFAULTS["coverage_scope"])
        ).strip().lower()
        if scope not in {"full", "local"}:
            scope = "full"
        return cls(
            coverage_scope=scope,
            min_canonical_coverage_full=_as_float(
                section.get("min_canonical_coverage_full"),
                _as_float(OUTLINE_QUALITY_GATE_DEFAULTS["min_canonical_coverage_full"]),
            ),
            min_canonical_coverage_local=_as_float(
                section.get("min_canonical_coverage_local"),
                _as_float(OUTLINE_QUALITY_GATE_DEFAULTS["min_canonical_coverage_local"]),
            ),
            min_effective_sections=_as_int(
                section.get("min_effective_sections"),
                _as_int(OUTLINE_QUALITY_GATE_DEFAULTS["min_effective_sections"]),
            ),
            max_duplicate_assignments=_as_int(
                section.get("max_duplicate_assignments"),
                _as_int(OUTLINE_QUALITY_GATE_DEFAULTS["max_duplicate_assignments"]),
            ),
            block_placeholder_sections=_as_bool(
                section.get("block_placeholder_sections"),
                _as_bool(OUTLINE_QUALITY_GATE_DEFAULTS["block_placeholder_sections"]),
            ),
            block_empty_research_streams=_as_bool(
                section.get("block_empty_research_streams"),
                _as_bool(OUTLINE_QUALITY_GATE_DEFAULTS["block_empty_research_streams"]),
            ),
        )

    @property
    def min_canonical_coverage(self) -> float:
        if self.coverage_scope == "local":
            return self.min_canonical_coverage_local
        return self.min_canonical_coverage_full

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["min_canonical_coverage"] = self.min_canonical_coverage
        return payload


@dataclass(frozen=True)
class OutlineV2Config:
    """Parsed and validated v2 configuration snapshot."""

    # Master switch
    enable_outline_intelligence_v2: bool = False

    # Sub-switches (only take effect when master switch is on)
    enable_literature_map: bool = True
    enable_synthesis_flow: bool = True
    enable_multi_model_critique: bool = True
    enable_coverage_audit: bool = True
    require_explicit_adopt: bool = True
    allow_bibliometric_provider: bool = False

    # Candidate count
    candidate_count: int = 3
    max_candidate_count: int = 3

    # Model routes
    outline_model: str = "Outline_API"
    structure_critic_model: str = "Writer_API"
    coverage_critic_model: str = "Primary_Reader_API"
    arbitrator_model: str = "Outline_API"

    # Cost control
    max_critique_models: int = 2
    max_summary_refs_per_prompt: int = 80
    max_outline_retry_count: int = 2

    # Mode
    is_test_fixture_mode: bool = False

    # Validation errors collected during parsing
    validation_errors: tuple = ()

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        is_test_fixture_mode: bool = False,
    ) -> "OutlineV2Config":
        """Parse v2 config from a ConfigDict or dict-like config mapping."""
        outline = dict(config.get("Outline", {})) if config else {}
        models_section = dict(config.get("OutlineModels", {})) if config else {}
        cost_section = dict(config.get("OutlineCostControl", {})) if config else {}

        errors: list[str] = []

        master = _as_bool(
            outline.get("enable_outline_intelligence_v2"),
            default=_as_bool(OUTLINE_DEFAULTS["enable_outline_intelligence_v2"]),
        )

        candidate_count = _as_int(
            outline.get("candidate_count"),
            default=_as_int(OUTLINE_DEFAULTS["candidate_count"]),
        )

        max_candidate = _as_int(
            cost_section.get("max_candidate_count"),
            default=_as_int(OUTLINE_COST_CONTROL_DEFAULTS["max_candidate_count"]),
        )

        # Validate candidate count bounds for production v2
        if master and not is_test_fixture_mode:
            if candidate_count < PRODUCTION_MIN_CANDIDATE_COUNT:
                errors.append(
                    f"candidate_count={candidate_count} below production minimum "
                    f"{PRODUCTION_MIN_CANDIDATE_COUNT}"
                )
            if candidate_count > PRODUCTION_MAX_CANDIDATE_COUNT:
                errors.append(
                    f"candidate_count={candidate_count} exceeds production maximum "
                    f"{PRODUCTION_MAX_CANDIDATE_COUNT}"
                )

        return cls(
            enable_outline_intelligence_v2=master,
            enable_literature_map=_as_bool(
                outline.get("enable_literature_map"),
                default=_as_bool(OUTLINE_DEFAULTS["enable_literature_map"]),
            ),
            enable_synthesis_flow=_as_bool(
                outline.get("enable_synthesis_flow"),
                default=_as_bool(OUTLINE_DEFAULTS["enable_synthesis_flow"]),
            ),
            enable_multi_model_critique=_as_bool(
                outline.get("enable_multi_model_critique"),
                default=_as_bool(OUTLINE_DEFAULTS["enable_multi_model_critique"]),
            ),
            enable_coverage_audit=_as_bool(
                outline.get("enable_coverage_audit"),
                default=_as_bool(OUTLINE_DEFAULTS["enable_coverage_audit"]),
            ),
            require_explicit_adopt=_as_bool(
                outline.get("require_explicit_adopt"),
                default=_as_bool(OUTLINE_DEFAULTS["require_explicit_adopt"]),
            ),
            allow_bibliometric_provider=_as_bool(
                outline.get("allow_bibliometric_provider"),
                default=_as_bool(OUTLINE_DEFAULTS["allow_bibliometric_provider"]),
            ),
            candidate_count=candidate_count,
            max_candidate_count=max_candidate,
            outline_model=str(
                models_section.get("outline_model", OUTLINE_MODEL_DEFAULTS["outline_model"])
            ),
            structure_critic_model=str(
                models_section.get(
                    "structure_critic_model",
                    OUTLINE_MODEL_DEFAULTS["structure_critic_model"],
                )
            ),
            coverage_critic_model=str(
                models_section.get(
                    "coverage_critic_model",
                    OUTLINE_MODEL_DEFAULTS["coverage_critic_model"],
                )
            ),
            arbitrator_model=str(
                models_section.get(
                    "arbitrator_model", OUTLINE_MODEL_DEFAULTS["arbitrator_model"]
                )
            ),
            max_critique_models=_as_int(
                cost_section.get("max_critique_models"),
                default=_as_int(OUTLINE_COST_CONTROL_DEFAULTS["max_critique_models"]),
            ),
            max_summary_refs_per_prompt=_as_int(
                cost_section.get("max_summary_refs_per_prompt"),
                default=_as_int(OUTLINE_COST_CONTROL_DEFAULTS["max_summary_refs_per_prompt"]),
            ),
            max_outline_retry_count=_as_int(
                cost_section.get("max_outline_retry_count"),
                default=_as_int(OUTLINE_COST_CONTROL_DEFAULTS["max_outline_retry_count"]),
            ),
            is_test_fixture_mode=is_test_fixture_mode,
            validation_errors=tuple(errors),
        )

    @property
    def is_valid_for_production(self) -> bool:
        """True if config passes production v2 validation."""
        return len(self.validation_errors) == 0

    @property
    def candidate_count_valid(self) -> bool:
        """True if candidate_count is within [min, max] for production."""
        if not self.enable_outline_intelligence_v2:
            return True
        return (
            PRODUCTION_MIN_CANDIDATE_COUNT
            <= self.candidate_count
            <= PRODUCTION_MAX_CANDIDATE_COUNT
        )

    def effective_candidate_count(self) -> int:
        """Return the configured candidate count without production coercion."""
        return self.candidate_count
