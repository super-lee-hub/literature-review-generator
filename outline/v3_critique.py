"""Typed, deterministic disposition of Outline v3 critique results.

Provider prose is retained as a message only. Candidate identity is resolved
from typed target IDs and the parent candidate hash, or from the executor's
trusted local identity on a candidate-shard result.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, TypedDict

CRITIQUE_DISPOSITION_VERSION = "outline_critique_disposition/v1"

_SCOPES = {"global", "candidate", "section", "claim"}
_SEVERITIES = {"blocking", "non_blocking"}
_RESOLUTION_STATUSES = {"unresolved", "deferred", "rejected", "resolved", "not_applicable"}
_BLOCKING_RESOLUTION_STATUSES = {"unresolved", "deferred", "rejected"}


class CritiqueIssue(TypedDict):
    issue_id: str
    scope: str
    target_ids: list[str]
    severity: str
    evidence_refs: list[str]
    resolution_status: str
    parent_candidate_hash: str
    candidate_id: str
    source_critic: str
    source: str
    message: str


class CritiqueDisposition(TypedDict):
    schema_version: str
    global_blocker: bool
    blocked_candidate_ids: list[str]
    eligible_candidate_ids: list[str]
    issues: list[CritiqueIssue]


def derive_critique_disposition(
    critiques: Mapping[str, Any],
    *,
    candidate_hashes: Mapping[str, str],
    candidate_contents: Mapping[str, Any],
    trusted_shard_critic_ids: Iterable[str] = (),
) -> CritiqueDisposition:
    """Return the exact candidate block/eligibility decision for critique payloads.

    A normal critique payload has a required boolean ``passed`` and an optional
    ``issues`` list. Each issue has ``issue_id``, ``scope``, ``target_ids``,
    ``severity``, ``evidence_refs``, ``resolution_status``, and
    ``parent_candidate_hash``. Global issues have no target IDs or candidate
    hash. Candidate, section, and claim issues must bind to exactly one known
    candidate through ``parent_candidate_hash`` and use exact target IDs.

    A locally merged payload may additionally contain
    ``candidate_shard_results``. Such rows are trusted only when the caller
    includes that critic ID in ``trusted_shard_critic_ids``; the caller must
    establish this provenance from the local execution/cache path, never from
    fields in the payload itself. Trusted rows carry a locally attached
    ``candidate_id``, a ``shard_id`` or ``shard_index``, and exact
    ``reviewed_section_ids``. A failed row without typed issues becomes a
    candidate-scoped blocker using that trusted identity. An untrusted payload
    containing ``candidate_shard_results`` causes a global blocker, and none of
    its row fields are read. Legacy diagnostic text is never inspected for
    candidate IDs.

    The return value contains only JSON-serializable values. Malformed identity,
    contradictory verdicts, incomplete shard coverage, and unscoped failures
    add a global blocker and leave no candidate eligible.
    """

    issues: list[CritiqueIssue] = []
    seen_issue_ids: set[str] = set()
    blocked: set[str] = set()
    global_blocker = False

    normalized_hashes, hashes_valid = _normalize_string_map(candidate_hashes)
    normalized_contents, contents_valid = _normalize_map(candidate_contents)
    trusted_shard_ids, trusted_shard_ids_valid = _normalize_trusted_critic_ids(
        trusted_shard_critic_ids
    )
    candidate_ids = sorted(normalized_hashes)
    candidate_id_set = set(candidate_ids)
    hash_to_candidate: dict[str, str] = {}

    if not candidate_ids:
        global_blocker = True
        _append_synthetic_issue(
            issues,
            seen_issue_ids,
            source_critic="input",
            locator="candidate_hashes",
            message="candidate hash map is empty or malformed",
        )
    if not hashes_valid:
        global_blocker = True
        _append_synthetic_issue(
            issues,
            seen_issue_ids,
            source_critic="input",
            locator="candidate_hashes",
            message="candidate hash identities must be unique non-empty strings",
        )
    if not trusted_shard_ids_valid:
        global_blocker = True
        _append_synthetic_issue(
            issues,
            seen_issue_ids,
            source_critic="input",
            locator="trusted_shard_critic_ids",
            message="trusted shard critic IDs must be a collection of unique non-empty strings",
        )
    for candidate_id, parent_hash in normalized_hashes.items():
        if parent_hash in hash_to_candidate:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic="input",
                locator=f"candidate_hashes:{candidate_id}",
                message="parent candidate hash is not unique",
            )
        else:
            hash_to_candidate[parent_hash] = candidate_id

    if not contents_valid or set(normalized_contents) != candidate_id_set:
        global_blocker = True
        _append_synthetic_issue(
            issues,
            seen_issue_ids,
            source_critic="input",
            locator="candidate_contents",
            message="candidate contents must match the candidate hash identities exactly",
        )

    sections_by_candidate: dict[str, set[str]] = {}
    claims_by_candidate: dict[str, set[str]] = {}
    for candidate_id in candidate_ids:
        content = normalized_contents.get(candidate_id)
        if not isinstance(content, Mapping):
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic="input",
                locator=f"candidate_contents:{candidate_id}",
                message="candidate content must be an object",
            )
            sections_by_candidate[candidate_id] = set()
            claims_by_candidate[candidate_id] = set()
            continue
        declared_candidate_id = content.get("candidate_id")
        if declared_candidate_id is not None and declared_candidate_id != candidate_id:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic="input",
                locator=f"candidate_contents:{candidate_id}:candidate_id",
                message="candidate content declares a different candidate identity",
            )
        section_ids, section_error = _section_ids(content)
        claim_ids, claim_error = _claim_ids(content)
        sections_by_candidate[candidate_id] = section_ids
        claims_by_candidate[candidate_id] = claim_ids
        if section_error:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic="input",
                locator=f"candidate_contents:{candidate_id}:sections",
                message=section_error,
            )
        if claim_error:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic="input",
                locator=f"candidate_contents:{candidate_id}:claims",
                message=claim_error,
            )

    if not isinstance(critiques, Mapping) or not critiques:
        global_blocker = True
        _append_synthetic_issue(
            issues,
            seen_issue_ids,
            source_critic="input",
            locator="critiques",
            message="at least one critique result is required",
        )
        critiques_to_process: Mapping[str, Any] = {}
    else:
        critiques_to_process = critiques

    for critic_id in sorted(critiques_to_process, key=lambda value: str(value)):
        if not isinstance(critic_id, str) or not critic_id.strip():
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic="input",
                locator="critiques:critic_id",
                message="critique identity must be a non-empty string",
            )
            continue
        raw_critique = critiques_to_process[critic_id]
        if not isinstance(raw_critique, Mapping):
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator="payload",
                message="critique payload must be an object",
            )
            continue

        passed = raw_critique.get("passed")
        passed_valid = type(passed) is bool
        if not passed_valid:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator="passed",
                message="critique passed field must be a boolean",
            )

        raw_issues, issues_present, issues_valid = _read_issues(raw_critique)
        if not issues_valid:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator="issues",
                message="critique issues must be a list of typed issue objects",
            )
        elif issues_present:
            for issue_index, raw_issue in enumerate(raw_issues):
                parsed, error = _parse_typed_issue(
                    raw_issue,
                    source_critic=critic_id,
                    locator=f"issues:{issue_index}",
                    candidate_ids=candidate_id_set,
                    hash_to_candidate=hash_to_candidate,
                    sections_by_candidate=sections_by_candidate,
                    claims_by_candidate=claims_by_candidate,
                )
                if error:
                    global_blocker = True
                    _append_synthetic_issue(
                        issues,
                        seen_issue_ids,
                        source_critic=critic_id,
                        locator=f"issues:{issue_index}",
                        message=error,
                    )
                    continue
                assert parsed is not None
                if not _append_issue(issues, seen_issue_ids, parsed):
                    global_blocker = True
                    _append_synthetic_issue(
                        issues,
                        seen_issue_ids,
                        source_critic=critic_id,
                        locator=f"issues:{issue_index}:duplicate",
                        message="critique issue_id is duplicated",
                    )

        shard_field_present = "candidate_shard_results" in raw_critique
        shard_results_trusted = shard_field_present and critic_id in trusted_shard_ids
        if shard_field_present and not shard_results_trusted:
            # Do not inspect row identities, verdicts, or prose from this field.
            # A flat provider result cannot self-assert the local provenance
            # needed for shard-scoped rejection.
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator="candidate_shard_results:untrusted",
                message="candidate_shard_results requires caller-verified local provenance",
            )
            shard_blocked_candidates: set[str] = set()
            shard_global = False
            shard_failed = False
        elif shard_results_trusted:
            shard_blocked_candidates, shard_global, shard_failed = _parse_candidate_shards(
                raw_critique.get("candidate_shard_results"),
                critic_id=critic_id,
                candidate_ids=candidate_id_set,
                normalized_hashes=normalized_hashes,
                sections_by_candidate=sections_by_candidate,
                claims_by_candidate=claims_by_candidate,
                hash_to_candidate=hash_to_candidate,
                issues=issues,
                seen_issue_ids=seen_issue_ids,
            )
        else:
            shard_blocked_candidates = set()
            shard_global = False
            shard_failed = False
        blocked.update(shard_blocked_candidates)
        global_blocker = global_blocker or shard_global

        shard_results_present = shard_results_trusted
        if shard_results_present and passed_valid:
            if passed and shard_failed:
                global_blocker = True
                _append_synthetic_issue(
                    issues,
                    seen_issue_ids,
                    source_critic=critic_id,
                    locator="candidate_shard_results:verdict_mismatch",
                    message="aggregate critique passed while a candidate shard failed",
                )
            if not passed and not shard_failed:
                global_blocker = True
                _append_synthetic_issue(
                    issues,
                    seen_issue_ids,
                    source_critic=critic_id,
                    locator="candidate_shard_results:verdict_mismatch",
                    message="aggregate critique failed without a failed candidate shard",
                )

        legacy_diagnostics, diagnostics_present, diagnostics_valid = _read_diagnostics(raw_critique)
        if not diagnostics_valid:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator="blocking_diagnostics",
                message="blocking_diagnostics must be a list of strings",
            )
        elif diagnostics_present and any(item.strip() for item in legacy_diagnostics):
            if shard_results_present:
                local_diagnostics = _shard_diagnostics(raw_critique.get("candidate_shard_results"))
                if local_diagnostics is None or sorted(legacy_diagnostics) != sorted(local_diagnostics):
                    global_blocker = True
                    _append_synthetic_issue(
                        issues,
                        seen_issue_ids,
                        source_critic=critic_id,
                        locator="blocking_diagnostics:unscoped",
                        message="aggregate prose is not fully explained by trusted shard results",
                    )
            else:
                global_blocker = True
                _append_synthetic_issue(
                    issues,
                    seen_issue_ids,
                    source_critic=critic_id,
                    locator="blocking_diagnostics:unscoped",
                    message="legacy critique prose cannot establish candidate scope",
                )

        active_for_critic = [
            issue for issue in issues
            if issue.get("source_critic") == critic_id and _is_active_blocker(issue)
        ]
        typed_active_for_critic = [
            issue for issue in active_for_critic
            if issue.get("source") in {"typed", "trusted_candidate_shard"}
        ]
        if passed_valid and not passed:
            if shard_results_present and shard_failed:
                # Failed rows carry a local candidate_id assigned by the call
                # planner. Their verdict may safely scope the synthesized issue.
                pass
            elif not typed_active_for_critic:
                global_blocker = True
                _append_synthetic_issue(
                    issues,
                    seen_issue_ids,
                    source_critic=critic_id,
                    locator="passed:false:unscoped",
                    message="non-passing critique has no valid typed scope",
                )
        elif passed_valid and passed and typed_active_for_critic:
            global_blocker = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator="passed:true:contradiction",
                message="passing critique contains an unresolved blocking issue",
            )

    for issue in issues:
        if _is_active_blocker(issue):
            if issue["scope"] == "global":
                global_blocker = True
            else:
                candidate_id = issue.get("candidate_id")
                if isinstance(candidate_id, str) and candidate_id in candidate_id_set:
                    blocked.add(candidate_id)
                else:
                    global_blocker = True

    blocked_ids = candidate_ids if global_blocker else sorted(blocked)
    eligible_ids = [] if global_blocker else [item for item in candidate_ids if item not in blocked]
    return {
        "schema_version": CRITIQUE_DISPOSITION_VERSION,
        "global_blocker": bool(global_blocker),
        "blocked_candidate_ids": list(blocked_ids),
        "eligible_candidate_ids": list(eligible_ids),
        "issues": sorted(issues, key=_issue_sort_key),
    }


def _normalize_string_map(value: Any) -> tuple[dict[str, str], bool]:
    if not isinstance(value, Mapping):
        return {}, False
    normalized: dict[str, str] = {}
    valid = True
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip():
            valid = False
            continue
        if key in normalized:
            valid = False
        normalized[key] = item
    return normalized, valid


def _normalize_map(value: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, Mapping):
        return {}, False
    normalized: dict[str, Any] = {}
    valid = True
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            valid = False
            continue
        if key in normalized:
            valid = False
        normalized[key] = item
    return normalized, valid


def _normalize_trusted_critic_ids(value: Any) -> tuple[set[str], bool]:
    if (
        not isinstance(value, Iterable)
        or isinstance(value, (str, bytes, Mapping))
    ):
        return set(), False
    rows = list(value)
    valid = all(isinstance(item, str) and item.strip() for item in rows)
    valid = valid and len(rows) == len({item for item in rows if isinstance(item, str)})
    if not valid:
        return set(), False
    return set(rows), True


def _section_ids(content: Mapping[str, Any]) -> tuple[set[str], str]:
    sections = content.get("sections")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        return set(), "candidate sections must be a list"
    found: set[str] = set()
    for item in sections:
        if not isinstance(item, Mapping):
            return set(), "candidate section must be an object"
        section_id = item.get("section_id")
        if not isinstance(section_id, str) or not section_id.strip():
            return set(), "candidate section_id must be a non-empty string"
        if section_id in found:
            return set(), "candidate section_id is duplicated"
        found.add(section_id)
    return found, ""


def _claim_ids(content: Mapping[str, Any]) -> tuple[set[str], str]:
    found: set[str] = set()
    malformed = False

    def visit(value: Any) -> None:
        nonlocal malformed
        if isinstance(value, Mapping):
            claim_id = value.get("claim_id")
            if claim_id is not None:
                if not isinstance(claim_id, str) or not claim_id.strip():
                    malformed = True
                else:
                    found.add(claim_id)
            for child in value.values():
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(content)
    if malformed:
        return set(), "claim_id values must be non-empty strings"
    return found, ""


def _read_issues(payload: Mapping[str, Any]) -> tuple[list[Any], bool, bool]:
    present = "issues" in payload
    if not present:
        return [], False, True
    value = payload.get("issues")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], True, False
    return list(value), True, all(isinstance(item, Mapping) for item in value)


def _read_diagnostics(payload: Mapping[str, Any]) -> tuple[list[str], bool, bool]:
    present = "blocking_diagnostics" in payload
    if not present:
        return [], False, True
    value = payload.get("blocking_diagnostics")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], True, False
    rows = list(value)
    return [item for item in rows if isinstance(item, str)], True, all(isinstance(item, str) for item in rows)


def _parse_typed_issue(
    raw_issue: Any,
    *,
    source_critic: str,
    locator: str,
    candidate_ids: set[str],
    hash_to_candidate: Mapping[str, str],
    sections_by_candidate: Mapping[str, set[str]],
    claims_by_candidate: Mapping[str, set[str]],
    trusted_candidate_id: str | None = None,
) -> tuple[CritiqueIssue | None, str]:
    if not isinstance(raw_issue, Mapping):
        return None, "critique issue must be an object"
    issue_id = raw_issue.get("issue_id")
    scope = raw_issue.get("scope")
    severity = raw_issue.get("severity")
    resolution = raw_issue.get("resolution_status")
    parent_hash = raw_issue.get("parent_candidate_hash", "")
    target_ids, targets_valid = _string_list(raw_issue.get("target_ids"), allow_empty=scope == "global")
    evidence_refs, evidence_valid = _string_list(raw_issue.get("evidence_refs"), allow_empty=True)
    if not isinstance(issue_id, str) or not issue_id.strip():
        return None, "critique issue_id must be a non-empty string"
    if scope not in _SCOPES:
        return None, "critique issue scope is unknown"
    if severity not in _SEVERITIES:
        return None, "critique issue severity is unknown"
    if resolution not in _RESOLUTION_STATUSES:
        return None, "critique issue resolution_status is unknown"
    if not targets_valid or not evidence_valid:
        return None, "critique target_ids and evidence_refs must be lists of unique non-empty strings"
    if scope == "global":
        if target_ids or parent_hash not in ("", None):
            return None, "global critique issue cannot bind candidate targets or parent hash"
        candidate_id = ""
    else:
        if not isinstance(parent_hash, str) or not parent_hash:
            return None, "scoped critique issue requires parent_candidate_hash"
        candidate_id = hash_to_candidate.get(parent_hash, "")
        if not candidate_id or candidate_id not in candidate_ids:
            return None, "critique issue parent_candidate_hash is unknown or ambiguous"
        if trusted_candidate_id is not None and candidate_id != trusted_candidate_id:
            return None, "critique issue parent hash does not match trusted shard candidate_id"
        declared_candidate_id = raw_issue.get("candidate_id")
        if declared_candidate_id is not None and declared_candidate_id != candidate_id:
            return None, "critique issue candidate_id does not match parent_candidate_hash"
        if scope == "candidate":
            if target_ids != [candidate_id]:
                return None, "candidate-scoped issue must target exactly its parent candidate_id"
        elif scope == "section":
            allowed = sections_by_candidate.get(candidate_id, set())
            if not target_ids or not set(target_ids).issubset(allowed):
                return None, "section-scoped issue targets an unknown section_id"
        elif scope == "claim":
            allowed = claims_by_candidate.get(candidate_id, set())
            if not target_ids or not set(target_ids).issubset(allowed):
                return None, "claim-scoped issue targets an unknown claim_id"
    if trusted_candidate_id is not None and scope == "global":
        # Global findings remain global even when emitted by a local shard.
        candidate_id = ""
    message = raw_issue.get("message", "")
    if not isinstance(message, str):
        return None, "critique issue message must be a string"
    return {
        "issue_id": issue_id,
        "scope": scope,
        "target_ids": list(target_ids),
        "severity": severity,
        "evidence_refs": list(evidence_refs),
        "resolution_status": resolution,
        "parent_candidate_hash": parent_hash or "",
        "candidate_id": candidate_id,
        "source_critic": source_critic,
        "source": "typed",
        "message": message,
    }, ""


def _parse_candidate_shards(
    raw_shards: Any,
    *,
    critic_id: str,
    candidate_ids: set[str],
    normalized_hashes: Mapping[str, str],
    sections_by_candidate: Mapping[str, set[str]],
    claims_by_candidate: Mapping[str, set[str]],
    hash_to_candidate: Mapping[str, str],
    issues: list[CritiqueIssue],
    seen_issue_ids: set[str],
) -> tuple[set[str], bool, bool]:
    if raw_shards is None:
        return set(), False, False
    if isinstance(raw_shards, Mapping):
        rows = list(raw_shards.items())
    elif isinstance(raw_shards, Sequence) and not isinstance(raw_shards, (str, bytes)):
        rows = [("", item) for item in raw_shards]
    else:
        _append_synthetic_issue(
            issues,
            seen_issue_ids,
            source_critic=critic_id,
            locator="candidate_shard_results",
            message="candidate_shard_results must be an object or list",
        )
        return set(), True, False

    if not rows:
        _append_synthetic_issue(
            issues,
            seen_issue_ids,
            source_critic=critic_id,
            locator="candidate_shard_results:empty",
            message="candidate_shard_results cannot be empty",
        )
        return set(), True, False

    failed_candidates: set[str] = set()
    seen_shards: set[tuple[str, str]] = set()
    covered_sections: dict[str, set[str]] = {candidate_id: set() for candidate_id in candidate_ids}
    seen_candidates: set[str] = set()
    malformed = False

    for row_index, (row_key, raw_row) in enumerate(rows):
        locator = f"candidate_shard_results:{row_index}"
        if not isinstance(raw_row, Mapping):
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=locator,
                message="candidate shard result must be an object",
            )
            continue
        candidate_id = raw_row.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in candidate_ids:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:candidate_id",
                message="candidate shard result has an unknown or missing trusted candidate_id",
            )
            continue
        seen_candidates.add(candidate_id)
        shard_id = raw_row.get("shard_id")
        shard_index = raw_row.get("shard_index")
        if isinstance(shard_id, str) and shard_id.strip():
            shard_identity = f"id:{shard_id}"
            key_identity = shard_id
        elif type(shard_index) is int and shard_index > 0:
            shard_identity = f"index:{shard_index}"
            key_identity = str(shard_index)
        else:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:shard_identity",
                message="candidate shard requires a non-empty shard_id or positive shard_index",
            )
            continue
        composite = (candidate_id, shard_identity)
        if composite in seen_shards:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:collision",
                message="candidate shard identity collides within one candidate",
            )
        seen_shards.add(composite)
        if isinstance(raw_shards, Mapping):
            allowed_keys = {
                key_identity,
                f"{candidate_id}:shard:{key_identity}",
                f"{candidate_id}:{key_identity}",
            }
            if not isinstance(row_key, str) or row_key not in allowed_keys:
                malformed = True
                _append_synthetic_issue(
                    issues,
                    seen_issue_ids,
                    source_critic=critic_id,
                    locator=f"{locator}:key",
                    message="candidate shard map key disagrees with its locally attached identity",
                )

        reviewed_sections, reviewed_valid = _string_list(
            raw_row.get("reviewed_section_ids"),
            allow_empty=False,
        )
        expected_sections = sections_by_candidate.get(candidate_id, set())
        if (
            not reviewed_valid
            or not expected_sections
            or not set(reviewed_sections).issubset(expected_sections)
        ):
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:reviewed_section_ids",
                message="candidate shard reviewed_section_ids are missing or outside its candidate",
            )
        else:
            covered_sections[candidate_id].update(reviewed_sections)

        row_parent_hash = raw_row.get("parent_candidate_hash")
        expected_parent_hash = normalized_hashes.get(candidate_id, "")
        if row_parent_hash is not None and row_parent_hash != expected_parent_hash:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:parent_candidate_hash",
                message="candidate shard parent hash disagrees with the trusted candidate hash",
            )

        row_passed = raw_row.get("passed")
        if type(row_passed) is not bool:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:passed",
                message="candidate shard passed field must be a boolean",
            )
            continue

        raw_issues, issues_present, issues_valid = _read_issues(raw_row)
        if not issues_valid:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:issues",
                message="candidate shard issues must be a list of typed issue objects",
            )
            continue
        row_issue_indexes: list[int] = []
        if issues_present:
            for issue_index, raw_issue in enumerate(raw_issues):
                parsed, error = _parse_typed_issue(
                    raw_issue,
                    source_critic=critic_id,
                    locator=f"{locator}:issues:{issue_index}",
                    candidate_ids=candidate_ids,
                    hash_to_candidate=hash_to_candidate,
                    sections_by_candidate=sections_by_candidate,
                    claims_by_candidate=claims_by_candidate,
                    trusted_candidate_id=candidate_id,
                )
                if error:
                    malformed = True
                    _append_synthetic_issue(
                        issues,
                        seen_issue_ids,
                        source_critic=critic_id,
                        locator=f"{locator}:issues:{issue_index}",
                        message=error,
                    )
                    continue
                assert parsed is not None
                if _append_issue(issues, seen_issue_ids, parsed):
                    row_issue_indexes.append(len(issues) - 1)
                else:
                    malformed = True
                    _append_synthetic_issue(
                        issues,
                        seen_issue_ids,
                        source_critic=critic_id,
                        locator=f"{locator}:issues:{issue_index}:duplicate",
                        message="critique issue_id is duplicated",
                    )

        row_diagnostics, diagnostics_present, diagnostics_valid = _read_diagnostics(raw_row)
        if not diagnostics_valid:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:blocking_diagnostics",
                message="candidate shard blocking_diagnostics must be a list of strings",
            )

        active_row_issues = [
            issues[index] for index in row_issue_indexes if _is_active_blocker(issues[index])
        ]
        if row_passed and (active_row_issues or (diagnostics_present and any(item.strip() for item in row_diagnostics))):
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"{locator}:verdict_mismatch",
                message="passing candidate shard contains an unresolved blocker",
            )
        if not row_passed:
            failed_candidates.add(candidate_id)
            if not active_row_issues:
                raw_message = "; ".join(item.strip() for item in row_diagnostics if item.strip())
                synthetic = _make_issue(
                    issue_id=_synthetic_issue_id(critic_id, locator, "failed_shard"),
                    scope="candidate",
                    target_ids=[candidate_id],
                    severity="blocking",
                    evidence_refs=[],
                    resolution_status="unresolved",
                    parent_candidate_hash=expected_parent_hash,
                    candidate_id=candidate_id,
                    source_critic=critic_id,
                    source="trusted_candidate_shard",
                    message=raw_message or "candidate shard returned passed=false",
                )
                if not _append_issue(issues, seen_issue_ids, synthetic):
                    malformed = True
                    _append_synthetic_issue(
                        issues,
                        seen_issue_ids,
                        source_critic=critic_id,
                        locator=f"{locator}:duplicate_failure",
                        message="candidate shard failure identity is duplicated",
                    )

    for candidate_id in sorted(candidate_ids):
        if candidate_id not in seen_candidates:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"candidate_shard_results:missing:{candidate_id}",
                message="candidate shard results omit a requested candidate",
            )
            continue
        expected_sections = sections_by_candidate.get(candidate_id, set())
        if expected_sections and covered_sections.get(candidate_id, set()) != expected_sections:
            malformed = True
            _append_synthetic_issue(
                issues,
                seen_issue_ids,
                source_critic=critic_id,
                locator=f"candidate_shard_results:coverage:{candidate_id}",
                message="candidate shard results do not cover every reviewed section exactly by identity",
            )

    return failed_candidates, malformed, bool(failed_candidates)


def _shard_diagnostics(raw_shards: Any) -> list[str] | None:
    if isinstance(raw_shards, Mapping):
        rows = list(raw_shards.values())
    elif isinstance(raw_shards, Sequence) and not isinstance(raw_shards, (str, bytes)):
        rows = list(raw_shards)
    else:
        return None
    diagnostics: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        values, present, valid = _read_diagnostics(row)
        if not valid:
            return None
        if present:
            diagnostics.extend(values)
    return diagnostics


def _string_list(value: Any, *, allow_empty: bool) -> tuple[list[str], bool]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], False
    rows = list(value)
    valid = all(isinstance(item, str) and item.strip() for item in rows)
    if not allow_empty and not rows:
        valid = False
    if len(rows) != len({item for item in rows if isinstance(item, str)}):
        valid = False
    return [item for item in rows if isinstance(item, str)], valid


def _make_issue(
    *,
    issue_id: str,
    scope: str,
    target_ids: list[str],
    severity: str,
    evidence_refs: list[str],
    resolution_status: str,
    parent_candidate_hash: str,
    candidate_id: str,
    source_critic: str,
    source: str,
    message: str,
) -> CritiqueIssue:
    return {
        "issue_id": issue_id,
        "scope": scope,
        "target_ids": list(target_ids),
        "severity": severity,
        "evidence_refs": list(evidence_refs),
        "resolution_status": resolution_status,
        "parent_candidate_hash": parent_candidate_hash,
        "candidate_id": candidate_id,
        "source_critic": source_critic,
        "source": source,
        "message": message,
    }


def _append_synthetic_issue(
    issues: list[CritiqueIssue],
    seen_issue_ids: set[str],
    *,
    source_critic: str,
    locator: str,
    message: str,
) -> None:
    issue = _make_issue(
        issue_id=_synthetic_issue_id(source_critic, locator, message),
        scope="global",
        target_ids=[],
        severity="blocking",
        evidence_refs=[],
        resolution_status="unresolved",
        parent_candidate_hash="",
        candidate_id="",
        source_critic=source_critic,
        source="validator",
        message=message,
    )
    _append_issue(issues, seen_issue_ids, issue)


def _append_issue(
    issues: list[CritiqueIssue],
    seen_issue_ids: set[str],
    issue: CritiqueIssue,
) -> bool:
    issue_id = str(issue.get("issue_id") or "")
    if not issue_id or issue_id in seen_issue_ids:
        return False
    seen_issue_ids.add(issue_id)
    issues.append(issue)
    return True


def _synthetic_issue_id(source_critic: str, locator: str, message: str) -> str:
    value = json.dumps(
        [source_critic, locator, message],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"validator:{hashlib.sha256(value).hexdigest()[:24]}"


def _is_active_blocker(issue: Mapping[str, Any]) -> bool:
    return (
        issue.get("severity") == "blocking"
        and issue.get("resolution_status") in _BLOCKING_RESOLUTION_STATUSES
    )


def _issue_sort_key(issue: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(issue.get("source_critic") or ""),
        str(issue.get("scope") or ""),
        str(issue.get("candidate_id") or ""),
        str(issue.get("issue_id") or ""),
        json.dumps(issue.get("target_ids") or [], ensure_ascii=False, separators=(",", ":")),
    )


__all__ = [
    "CRITIQUE_DISPOSITION_VERSION",
    "CritiqueDisposition",
    "CritiqueIssue",
    "derive_critique_disposition",
]
