from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
import hashlib
import json
import re
import unicodedata
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
_METADATA_AUTHOR_NOISE = {"cnki", "administrator", "wds", "rundonglee"}


def _normalized_pdf_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return text.translate(
        str.maketrans(
            {
                "‐": "-",
                "‑": "-",
                "‒": "-",
                "–": "-",
                "—": "-",
                "―": "-",
            }
        )
    )


def _compact_identity_text(value: Any) -> str:
    text = _normalized_pdf_text(value).casefold()
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text, flags=re.UNICODE)


def _title_variants(value: Any) -> tuple[str, ...]:
    raw = _normalized_pdf_text(value)
    variants: list[str] = []
    for candidate in (raw, re.split(r"[:：]", raw, maxsplit=1)[-1]):
        compact = _compact_identity_text(candidate)
        if compact and compact not in variants:
            variants.append(compact)
    return tuple(variants)


def _title_evidence_matches(expected_title: Any, source_text: Any) -> bool:
    variants = _title_variants(expected_title)
    if not variants:
        return False
    raw_text = _normalized_pdf_text(source_text)
    compact_text = _compact_identity_text(raw_text)
    if any(variant in compact_text for variant in variants):
        return True

    # A small OCR error (for example ``~onsumers``) or a broken title over
    # several PDF text lines should not turn a visibly exact title into a
    # missing source.  Acceptance still requires an author/year match in
    # evaluate_source_identity.
    lines = [
        _compact_identity_text(line)
        for line in raw_text.splitlines()
        if _compact_identity_text(line)
    ]
    for variant in variants:
        minimum_length = max(12, int(len(variant) * 0.65))
        for start in range(len(lines)):
            combined = ""
            for end in range(start, min(len(lines), start + 4)):
                combined += lines[end]
                if len(combined) > int(len(variant) * 1.35):
                    break
                if len(combined) < minimum_length:
                    continue
                ratio = difflib.SequenceMatcher(None, variant, combined).ratio()
                if ratio >= 0.88:
                    return True
    return False


def _is_expected_doi_artifact(candidate: str, expected_doi: str) -> bool:
    if not expected_doi or not candidate.startswith(expected_doi):
        return False
    suffix = candidate[len(expected_doi) :]
    return bool(
        re.fullmatch(r"/\d+", suffix)
        or re.fullmatch(r"(?:wileylogo|wiley|logo)", suffix, flags=re.IGNORECASE)
    )


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
        "year": _safe_year(value.get("year") or value.get("date")),
        "doi": normalize_doi(value.get("doi")),
    }


def _normalized_doi_candidates(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_candidates = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_candidates = list(value)
    else:
        raw_candidates = []
    candidates: list[str] = []
    for raw_candidate in raw_candidates:
        candidate = normalize_doi(raw_candidate)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


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
    observed_doi_candidates = _normalized_doi_candidates(
        observed.get("doi_candidates")
    )
    expected_doi = expected_value["doi"]
    observed_doi = observed_value["doi"]

    if len(observed_doi_candidates) > 1:
        verdict: IdentityVerdict = "ambiguous"
        reasons = ("multiple_distinct_doi_candidates",)
    elif expected_doi and observed_doi and expected_doi != observed_doi:
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
        author_match = bool(
            expected_authors
            and observed_authors
            and expected_authors[0] == observed_authors[0]
        )
        expected_year = expected_value["year"]
        observed_year = observed_value["year"]
        year_conflict = bool(expected_year and observed_year and expected_year != observed_year)
        year_match = bool(expected_year and observed_year and expected_year == observed_year)
        if (
            title_matches
            and not author_conflict
            and not year_conflict
            and (author_match or year_match)
        ):
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
    normalized_text = _compact_identity_text(first_page_text)
    year_evidence_text = _normalized_pdf_text(first_page_text).casefold()
    expected_title_key = _compact_identity_text(expected_value["title"])
    title_confirmed = _title_evidence_matches(expected_value["title"], first_page_text)
    title = metadata_title
    if title_confirmed:
        title = expected_value["title"]
    identity_evidence_text = normalized_text
    if title_confirmed and expected_title_key:
        identity_evidence_text = normalized_text.replace(expected_title_key, " ", 1)

    metadata_author = str(metadata.get("author") or "").strip()
    expected_authors = expected_value["authors"]
    expected_author_key = (
        _compact_identity_text(expected_authors[0]) if expected_authors else ""
    )
    first_author_is_observed = bool(
        expected_author_key and expected_author_key in identity_evidence_text
    )
    if first_author_is_observed:
        # Prefer the article's visible author over exporter/editor metadata.
        observed_authors = [expected_authors[0]]
    elif metadata_author and _compact_identity_text(metadata_author) not in _METADATA_AUTHOR_NOISE:
        observed_authors = [metadata_author]
    else:
        observed_authors = []

    expected_year = expected_value["year"]
    observed_year = (
        expected_year
        if expected_year
        and re.search(
            rf"(?<!\d){re.escape(expected_year)}(?!\d)", year_evidence_text
        )
        else ""
    )

    doi_candidates: list[str] = []
    expected_doi = normalize_doi(expected_value.get("doi"))
    for raw_value in [
        metadata.get("subject"),
        metadata.get("keywords"),
        first_page_text,
    ]:
        for match in _DOI_SEARCH.finditer(_normalized_pdf_text(raw_value)):
            doi = normalize_doi(match.group(0))
            if doi and doi not in doi_candidates:
                doi_candidates.append(doi)
    if expected_doi in doi_candidates:
        # Some PDF generators concatenate the text immediately following a
        # DOI with the DOI token (for example ``10.x/abcjournal``).  When the
        # exact expected DOI is independently present, discard only that
        # alphanumeric prefix-extension artifact; genuinely different DOI
        # candidates remain fail-closed ambiguous.
        doi_candidates = [
            candidate
            for candidate in doi_candidates
            if candidate == expected_doi
            or not (
                candidate.startswith(expected_doi)
                and bool(re.fullmatch(r"[a-z0-9]+", candidate[len(expected_doi):]))
            )
        ]
    elif len(doi_candidates) == 1 and _is_expected_doi_artifact(
        doi_candidates[0], expected_doi
    ):
        # Publisher PDF layout sometimes appends a logo token or an article
        # identifier to the DOI URL path (for example ``...70067WILEYlogo``
        # or ``.../5510554``).  Treat that as the expected DOI only when the
        # complete candidate has the expected DOI as its prefix and there is
        # no competing DOI candidate.
        doi_candidates = [expected_doi]
    observed_doi = doi_candidates[0] if len(doi_candidates) == 1 else ""

    return {
        "title": title,
        "authors": observed_authors,
        "year": observed_year,
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
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:  # pragma: no cover - compatibility with older PyMuPDF releases.
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
