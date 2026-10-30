"""Lossless local references for critique paper-role claim text."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PAPER_ROLE_CLAIM_ARRAY_FIELD_V1 = "candidate_contents.sections.claims"
PAPER_ROLE_CLAIM_INDEX_CONTRACT_V1 = (
    "Every integer paper_roles value has a paper_role_claim_array_ref with the "
    "exact candidate_id, section_id, and field "
    "candidate_contents.sections.claims. Resolve it against that section's "
    "claims array in candidate_contents, including when the role map is copied "
    "into section_evidence. String values remain exact literal role text. "
    "Never resolve indices across candidates or sections."
)


class CritiquePaperRoleProjectionError(ValueError):
    """Raised when a local paper-role claim reference is malformed."""


def project_critique_paper_roles_v1(
    candidate_id: str,
    section: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace exact role-text duplicates with indices into this section's claims.

    The enclosing candidate and section IDs plus the claim-array field are
    recorded once in ``paper_role_claim_array_ref``. Text that does not exactly
    match a local claim remains an unchanged literal role value. The input is
    never mutated.
    """

    if not isinstance(candidate_id, str) or not candidate_id:
        raise CritiquePaperRoleProjectionError("candidate_id is required")
    projected = dict(section)
    roles = section.get("paper_roles")
    if not isinstance(roles, Mapping) or not roles:
        return projected

    if "paper_role_claim_array_ref" in section:
        raise CritiquePaperRoleProjectionError(
            "paper-role claim reference is already present"
        )
    section_id = section.get("section_id")
    claims = section.get("claims")
    if not isinstance(section_id, str) or not section_id:
        raise CritiquePaperRoleProjectionError("section_id is required")
    if (
        not isinstance(claims, Sequence)
        or isinstance(claims, (str, bytes))
        or any(not isinstance(claim, str) for claim in claims)
    ):
        raise CritiquePaperRoleProjectionError(
            "section claims must be an array of exact strings"
        )

    claim_indices: dict[str, int] = {}
    for index, claim in enumerate(claims):
        claim_indices.setdefault(claim, index)

    projected_roles: dict[str, str | int] = {}
    encoded = False
    for paper_key, role_text in roles.items():
        if not isinstance(paper_key, str) or not paper_key:
            raise CritiquePaperRoleProjectionError(
                "paper_roles keys must be nonempty paper IDs"
            )
        if not isinstance(role_text, str):
            raise CritiquePaperRoleProjectionError(
                "paper_roles values must be exact text before projection"
            )
        claim_index = claim_indices.get(role_text)
        if claim_index is None:
            projected_roles[paper_key] = role_text
            continue
        projected_roles[paper_key] = claim_index
        encoded = True

    projected["paper_roles"] = projected_roles
    if encoded:
        projected["paper_role_claim_array_ref"] = {
            "candidate_id": candidate_id,
            "section_id": section_id,
            "field": PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
        }
    return projected


def expand_critique_paper_roles_v1(
    candidate_id: str,
    section: Mapping[str, Any],
) -> dict[str, Any]:
    """Expand local claim indices back into the exact paper-role strings."""

    roles = section.get("paper_roles")
    if not isinstance(roles, Mapping):
        raise CritiquePaperRoleProjectionError("paper_roles must be an object")

    has_indices = any(type(value) is int for value in roles.values())
    claim_ref = section.get("paper_role_claim_array_ref")
    if not has_indices and claim_ref is None:
        if any(not isinstance(value, str) for value in roles.values()):
            raise CritiquePaperRoleProjectionError(
                "unprojected paper role values must be text"
            )
        expanded = dict(section)
        expanded["paper_roles"] = dict(roles)
        return expanded

    section_id = section.get("section_id")
    expected_ref = {
        "candidate_id": candidate_id,
        "section_id": section_id,
        "field": PAPER_ROLE_CLAIM_ARRAY_FIELD_V1,
    }
    if not isinstance(claim_ref, Mapping) or dict(claim_ref) != expected_ref:
        raise CritiquePaperRoleProjectionError(
            "paper-role claim array reference does not match its enclosing section"
        )
    claims = section.get("claims")
    if (
        not isinstance(claims, Sequence)
        or isinstance(claims, (str, bytes))
        or any(not isinstance(claim, str) for claim in claims)
    ):
        raise CritiquePaperRoleProjectionError(
            "referenced section claims must be an array of exact strings"
        )

    expanded_roles: dict[str, str] = {}
    for paper_key, value in roles.items():
        if not isinstance(paper_key, str) or not paper_key:
            raise CritiquePaperRoleProjectionError(
                "paper_roles keys must be nonempty paper IDs"
            )
        if type(value) is int:
            if value < 0 or value >= len(claims):
                raise CritiquePaperRoleProjectionError(
                    "paper-role claim index is outside its local claims array"
                )
            expanded_roles[paper_key] = claims[value]
        elif isinstance(value, str):
            expanded_roles[paper_key] = value
        else:
            raise CritiquePaperRoleProjectionError(
                "paper-role value must be a local claim index or literal text"
            )

    expanded = dict(section)
    expanded["paper_roles"] = expanded_roles
    expanded.pop("paper_role_claim_array_ref", None)
    return expanded
