"""Shared fail-closed contracts for current Stage 1 visual authority."""

from __future__ import annotations

from typing import Any, Mapping, TypeGuard


VISUAL_OMISSION_SCOPES = frozenset(
    {"page_coverage", "raw_reinspection", "final_transport"}
)


def _is_exact_nonnegative_int(value: Any) -> TypeGuard[int]:
    return type(value) is int and value >= 0


def _as_array(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return None


def _string_array(value: Any) -> list[str] | None:
    values = _as_array(value)
    if values is None or any(type(item) is not str for item in values):
        return None
    return values


def validate_visual_coverage_semantics(
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return semantic issues shared by qualification and Registry gates.

    This validator deliberately does not decide whether an unresolved raw
    unit is reusable under a policy.  It only validates facts that must be
    true for every current coverage/qualification artifact, regardless of
    strict versus relaxed reuse policy.
    """

    issues: list[str] = []
    required_raw_count = payload.get("required_raw_reinspection_unit_count")
    closed_raw_count = payload.get("closed_raw_reinspection_unit_count")
    raw_units = _as_array(payload.get("raw_reinspection_units"))
    unresolved_ids = _string_array(
        payload.get("unresolved_raw_reinspection_unit_ids")
    )

    if not _is_exact_nonnegative_int(required_raw_count):
        issues.append("raw_reinspection_unit_count_invalid")
        required_raw_count = 0
    if not _is_exact_nonnegative_int(closed_raw_count):
        if "raw_reinspection_unit_count_invalid" not in issues:
            issues.append("raw_reinspection_unit_count_invalid")
        closed_raw_count = 0
    if raw_units is None or unresolved_ids is None:
        issues.append("raw_reinspection_closure_invalid")
        raw_units = raw_units or []
        unresolved_ids = unresolved_ids or []

    unit_ids: list[str] = []
    closed_flags: list[bool] = []
    raw_units_valid = True
    for item in raw_units:
        if not isinstance(item, Mapping):
            raw_units_valid = False
            continue
        unit_id = item.get("unit_id")
        closed = item.get("closed")
        if type(unit_id) is not str or not unit_id.strip() or type(closed) is not bool:
            raw_units_valid = False
            continue
        unit_ids.append(unit_id)
        closed_flags.append(closed)
    if (
        not raw_units_valid
        or len(raw_units) != required_raw_count
        or len(unit_ids) != len(raw_units)
        or len(set(unit_ids)) != len(unit_ids)
        or closed_raw_count > required_raw_count
        or sum(closed_flags) != closed_raw_count
        or len(unresolved_ids) != required_raw_count - closed_raw_count
        or unresolved_ids
        != [unit_id for unit_id, closed in zip(unit_ids, closed_flags) if not closed]
    ):
        issues.append("raw_reinspection_closure_invalid")

    unresolved_set = set(unresolved_ids)
    final_status = payload.get("final_raw_visual_recheck_status")
    evidence_status = payload.get("evidence_coverage_status")
    if unresolved_set and (
        final_status in {"complete", "not_required"}
        or final_status not in {"partial", "not_run_fallback"}
        or evidence_status == "complete"
        or evidence_status not in {"degraded", "incomplete"}
    ):
        issues.append("raw_reinspection_state_invalid")

    unit_by_id = {
        unit_id: item
        for unit_id, item in zip(unit_ids, raw_units)
        if isinstance(item, Mapping)
    }
    omission_values: list[Any] = []
    for field_name in ("omissions", "transport_omissions"):
        if field_name not in payload:
            continue
        omissions = _as_array(payload.get(field_name))
        if omissions is None:
            issues.append("transport_omission_contract_invalid")
            continue
        omission_values.extend(omissions)
    for omission in omission_values:
        if not isinstance(omission, Mapping):
            issues.append("transport_omission_contract_invalid")
            continue
        scope = omission.get("scope")
        authority_blocking = omission.get("authority_blocking")
        if (
            type(scope) is not str
            or scope not in VISUAL_OMISSION_SCOPES
            or type(authority_blocking) is not bool
            or (scope != "raw_reinspection" and authority_blocking is not True)
        ):
            issues.append("transport_omission_contract_invalid")
            continue
        if scope != "raw_reinspection":
            continue
        group_id = omission.get("raw_reinspection_group_id")
        if type(group_id) is not str or not group_id.strip() or group_id not in unit_by_id:
            issues.append("raw_reinspection_omission_unknown_unit")
            continue
        if (
            omission.get("raw_reinspection_resolution") == "not_represented"
            and unit_by_id[group_id].get("closed") is not False
        ):
            issues.append("raw_reinspection_not_represented_closed")

    return tuple(dict.fromkeys(issues))


_CURRENT_REQUIRED_FIELDS = (
    "artifact_type",
    "artifact_version",
    "coverage_artifact_id",
    "coverage_artifact_hash",
    "coverage_artifact_path",
    "observation_artifact_ids",
    "observation_artifact_hashes",
    "observation_artifact_paths",
    "required_nonblank_page_count",
    "required_page_ids",
    "sent_page_ids",
    "observed_page_ids",
    "render_failed_page_ids",
    "scan_failed_page_ids",
    "transport_omissions",
    "scan_coverage_status",
    "final_synthesis_modality",
    "final_raw_visual_recheck_status",
    "evidence_coverage_status",
    "required_raw_reinspection_unit_count",
    "closed_raw_reinspection_unit_count",
    "unresolved_raw_reinspection_unit_ids",
    "raw_reinspection_units",
    "require_complete_visual_coverage",
    "visual_observation_artifact_version",
    "visual_scan_prompt_id",
    "visual_scan_prompt_version",
    "visual_scan_prompt_sha256",
    "visual_scan_schema_hash",
)

_CURRENT_STRING_FIELDS = (
    "artifact_type",
    "artifact_version",
    "coverage_artifact_id",
    "coverage_artifact_hash",
    "coverage_artifact_path",
    "scan_coverage_status",
    "final_synthesis_modality",
    "final_raw_visual_recheck_status",
    "evidence_coverage_status",
    "visual_observation_artifact_version",
    "visual_scan_prompt_id",
    "visual_scan_prompt_version",
    "visual_scan_prompt_sha256",
    "visual_scan_schema_hash",
)
_CURRENT_STRING_ARRAY_FIELDS = (
    "observation_artifact_ids",
    "observation_artifact_hashes",
    "observation_artifact_paths",
    "required_page_ids",
    "sent_page_ids",
    "observed_page_ids",
    "render_failed_page_ids",
    "scan_failed_page_ids",
    "unresolved_raw_reinspection_unit_ids",
)
_CURRENT_INT_FIELDS = (
    "required_nonblank_page_count",
    "required_raw_reinspection_unit_count",
    "closed_raw_reinspection_unit_count",
)


def validate_current_visual_evidence_qualification(
    value: Any,
) -> tuple[str, ...]:
    """Validate the serialized JSON shape and semantics of current v1 data."""

    if not isinstance(value, Mapping):
        return ("qualification_mapping_invalid",)
    issues: list[str] = []
    missing = [field for field in _CURRENT_REQUIRED_FIELDS if field not in value]
    if missing:
        issues.append("qualification_fields_missing")
    for field_name in _CURRENT_STRING_FIELDS:
        if field_name in value and type(value[field_name]) is not str:
            issues.append("qualification_string_type_invalid")
    for field_name in _CURRENT_STRING_ARRAY_FIELDS:
        if field_name in value and _string_array(value[field_name]) is None:
            issues.append("qualification_array_type_invalid")
    for field_name in _CURRENT_INT_FIELDS:
        if field_name in value and not _is_exact_nonnegative_int(value[field_name]):
            issues.append("qualification_integer_type_invalid")
    if "require_complete_visual_coverage" in value and type(
        value["require_complete_visual_coverage"]
    ) is not bool:
        issues.append("qualification_policy_type_invalid")
    for field_name in ("raw_reinspection_units", "transport_omissions"):
        if field_name in value and not isinstance(value[field_name], list):
            issues.append("qualification_array_type_invalid")
    if "raw_reinspection_units" in value and isinstance(
        value["raw_reinspection_units"], list
    ) and any(not isinstance(item, Mapping) for item in value["raw_reinspection_units"]):
        issues.append("qualification_object_array_type_invalid")
    if "transport_omissions" in value and isinstance(
        value["transport_omissions"], list
    ) and any(not isinstance(item, Mapping) for item in value["transport_omissions"]):
        issues.append("qualification_object_array_type_invalid")
    if value.get("artifact_type") != "stage1_visual_evidence_qualification":
        issues.append("qualification_type_invalid")
    if value.get("artifact_version") != "v1":
        issues.append("qualification_version_invalid")

    if not issues:
        issues.extend(validate_visual_coverage_semantics(value))
    return tuple(dict.fromkeys(issues))


__all__ = [
    "VISUAL_OMISSION_SCOPES",
    "validate_current_visual_evidence_qualification",
    "validate_visual_coverage_semantics",
]
