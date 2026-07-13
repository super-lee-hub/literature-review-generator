from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal, Mapping

from services.paper_identity import (
    normalize_doi,
    normalized_author_surnames,
    normalized_title_key,
)


IdentityVerdict = Literal["match", "ambiguous", "mismatch"]
ArtifactStatus = Literal["ready", "quarantined", "invalid"]

IDENTITY_POLICY_VERSION = "source-identity-v1"
_DOI_SEARCH = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9&]+", re.IGNORECASE)


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def _normalize_identity_input(value: Mapping[str, Any]) -> dict[str, Any]:
    authors = value.get("authors")
    if isinstance(authors, str):
        normalized_authors = [authors]
    else:
        normalized_authors = [str(item) for item in (authors or []) if str(item).strip()]
    return {
        "title": str(value.get("title") or "").strip(),
        "authors": normalized_authors,
        "year": _safe_year(value.get("year")),
        "doi": normalize_doi(value.get("doi")),
    }


@dataclass(frozen=True)
class SourceIdentityResultV1:
    identity_verdict: IdentityVerdict
    artifact_status: ArtifactStatus
    expected: dict[str, Any]
    observed: dict[str, Any]
    policy_version: str = IDENTITY_POLICY_VERSION
    reasons: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    source_path: str = ""
    candidate_hash: str = ""
    evidence_hash: str = ""

    @property
    def canonical_ready(self) -> bool:
        return self.identity_verdict == "match" and self.artifact_status == "ready"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["diagnostics"] = list(self.diagnostics)
        payload["canonical_ready"] = self.canonical_ready
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceIdentityResultV1":
        verdict = str(payload.get("identity_verdict") or "ambiguous")
        status = str(payload.get("artifact_status") or "quarantined")
        if verdict not in {"match", "ambiguous", "mismatch"}:
            raise ValueError(f"unsupported identity verdict: {verdict}")
        if status not in {"ready", "quarantined", "invalid"}:
            raise ValueError(f"unsupported artifact status: {status}")
        return cls(
            identity_verdict=verdict,  # type: ignore[arg-type]
            artifact_status=status,  # type: ignore[arg-type]
            expected=dict(payload.get("expected") or {}),
            observed=dict(payload.get("observed") or {}),
            policy_version=str(payload.get("policy_version") or IDENTITY_POLICY_VERSION),
            reasons=tuple(str(item) for item in (payload.get("reasons") or [])),
            diagnostics=tuple(str(item) for item in (payload.get("diagnostics") or [])),
            source_path=str(payload.get("source_path") or ""),
            candidate_hash=str(payload.get("candidate_hash") or ""),
            evidence_hash=str(payload.get("evidence_hash") or ""),
        )


def evaluate_source_identity(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    source_path: str = "",
    candidate_hash: str = "",
    evidence_hash: str = "",
    diagnostics: tuple[str, ...] = (),
) -> SourceIdentityResultV1:
    expected_value = _normalize_identity_input(expected)
    observed_value = _normalize_identity_input(observed)
    expected_doi = expected_value["doi"]
    observed_doi = observed_value["doi"]

    if expected_doi and observed_doi and expected_doi != observed_doi:
        verdict: IdentityVerdict = "mismatch"
        reasons = ("nonempty_doi_mismatch",)
    elif expected_doi and observed_doi and expected_doi == observed_doi:
        verdict = "match"
        reasons = ("normalized_doi_match",)
    else:
        expected_title = normalized_title_key(expected_value["title"])
        observed_title = normalized_title_key(observed_value["title"])
        title_matches = (
            expected_title != "unknown_title"
            and expected_title == observed_title
        )
        expected_authors = normalized_author_surnames(expected_value["authors"])
        observed_authors = normalized_author_surnames(observed_value["authors"])
        author_conflict = bool(
            expected_authors
            and observed_authors
            and expected_authors[0] != observed_authors[0]
        )
        expected_year = expected_value["year"]
        observed_year = observed_value["year"]
        year_conflict = bool(expected_year and observed_year and expected_year != observed_year)
        if title_matches and not author_conflict and not year_conflict:
            verdict = "match"
            reasons = ("normalized_title_match_without_author_year_conflict",)
        else:
            verdict = "ambiguous"
            reason_items = []
            if not title_matches:
                reason_items.append("normalized_title_not_confirmed")
            if author_conflict:
                reason_items.append("author_conflict")
            if year_conflict:
                reason_items.append("year_conflict")
            reasons = tuple(reason_items or ["insufficient_identity_evidence"])

    return SourceIdentityResultV1(
        identity_verdict=verdict,
        artifact_status="ready" if verdict == "match" else "quarantined",
        expected=expected_value,
        observed=observed_value,
        reasons=reasons,
        diagnostics=diagnostics,
        source_path=str(Path(source_path).resolve()) if source_path else "",
        candidate_hash=candidate_hash,
        evidence_hash=evidence_hash,
    )


def _first_page_identity_observation(
    expected: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
    first_page_text: str,
) -> dict[str, Any]:
    expected_value = _normalize_identity_input(expected)
    metadata_title = str(metadata.get("title") or "").strip()
    normalized_text = normalized_title_key(first_page_text)
    expected_title_key = normalized_title_key(expected_value["title"])
    title = metadata_title
    if expected_title_key != "unknown_title" and expected_title_key in normalized_text:
        title = expected_value["title"]

    doi_candidates: list[str] = []
    for raw_value in [
        metadata.get("subject"),
        metadata.get("keywords"),
        first_page_text,
    ]:
        for match in _DOI_SEARCH.finditer(str(raw_value or "")):
            doi = normalize_doi(match.group(0))
            if doi and doi not in doi_candidates:
                doi_candidates.append(doi)
    expected_doi = expected_value["doi"]
    observed_doi = expected_doi if expected_doi in doi_candidates else (doi_candidates[0] if doi_candidates else "")

    return {
        "title": title,
        "authors": [str(metadata.get("author") or "").strip()] if metadata.get("author") else [],
        "year": "",
        "doi": observed_doi,
        "doi_candidates": doi_candidates,
    }


def inspect_pdf_identity(
    expected: Mapping[str, Any],
    pdf_path: str,
) -> SourceIdentityResultV1:
    path = Path(pdf_path).resolve()
    candidate_hash = ""
    diagnostics: list[str] = []
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        candidate_hash = digest.hexdigest()
    except OSError as exc:
        diagnostics.append(f"candidate_hash_error:{type(exc).__name__}")

    metadata: dict[str, Any] = {}
    first_page_text = ""
    try:
        import fitz  # type: ignore

        with fitz.open(str(path)) as document:
            metadata = dict(document.metadata or {})
            if document.page_count:
                first_page_text = str(document.load_page(0).get_text("text"))[:50000]
    except Exception as exc:
        diagnostics.append(f"pdf_identity_inspection_error:{type(exc).__name__}")

    observed = _first_page_identity_observation(
        expected,
        metadata=metadata,
        first_page_text=first_page_text,
    )
    evidence_hash = _stable_hash(
        {
            "metadata": metadata,
            "first_page_text": first_page_text,
        }
    )
    return evaluate_source_identity(
        expected,
        observed,
        source_path=str(path),
        candidate_hash=candidate_hash,
        evidence_hash=evidence_hash,
        diagnostics=tuple(diagnostics),
    )


def inspect_text_identity(
    expected: Mapping[str, Any],
    source_text: str,
    *,
    source_path: str = "",
    candidate_hash: str = "",
) -> SourceIdentityResultV1:
    observed = _first_page_identity_observation(
        expected,
        metadata={},
        first_page_text=str(source_text or "")[:50000],
    )
    evidence_hash = hashlib.sha256(str(source_text or "").encode("utf-8")).hexdigest()
    return evaluate_source_identity(
        expected,
        observed,
        source_path=source_path,
        candidate_hash=candidate_hash,
        evidence_hash=evidence_hash,
    )
