"""Deterministic, read-only PDF discovery for Zotero libraries."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
from types import MappingProxyType
import unicodedata
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexedPdf:
    path: str
    relative_path: str
    filename: str
    normalized_filename: str
    size_bytes: int


@dataclass(frozen=True)
class PdfMatchCandidateV1:
    path: str
    match_kind: Literal["attachment_relative", "attachment_basename", "title_fuzzy"]
    score: float
    score_components: Mapping[str, float] = field(default_factory=dict)
    diagnostics: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "match_kind": self.match_kind,
            "score": self.score,
            "score_components": dict(self.score_components),
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PdfMatchCandidateV1":
        match_kind = str(payload.get("match_kind") or "title_fuzzy")
        if match_kind not in {"attachment_relative", "attachment_basename", "title_fuzzy"}:
            match_kind = "title_fuzzy"
        components = payload.get("score_components")
        return cls(
            path=str(payload.get("path") or ""),
            match_kind=match_kind,  # type: ignore[arg-type]
            score=float(payload.get("score") or 0.0),
            score_components={
                str(key): float(value)
                for key, value in (components.items() if isinstance(components, Mapping) else [])
            },
            diagnostics=tuple(str(item) for item in (payload.get("diagnostics") or [])),
        )


@dataclass(frozen=True)
class PdfMatchResultV1:
    status: Literal["matched", "ambiguous", "not_found"]
    selected_path: str = ""
    candidates: Tuple[PdfMatchCandidateV1, ...] = ()
    diagnostics: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "selected_path": self.selected_path,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PdfMatchResultV1":
        status = str(payload.get("status") or "not_found")
        if status not in {"matched", "ambiguous", "not_found"}:
            status = "not_found"
        return cls(
            status=status,  # type: ignore[arg-type]
            selected_path=str(payload.get("selected_path") or ""),
            candidates=tuple(
                PdfMatchCandidateV1.from_dict(item)
                for item in (payload.get("candidates") or [])
                if isinstance(item, Mapping)
            ),
            diagnostics=tuple(str(item) for item in (payload.get("diagnostics") or [])),
        )


class FileIndex:
    """An immutable index scoped to exactly one normalized library root."""

    def __init__(self, library_path: str):
        if not library_path:
            raise ValueError("library_path must be a non-empty path")
        root = Path(library_path).expanduser().resolve()
        if not root.exists():
            raise OSError(f"Zotero storage path does not exist: {root}")
        if not root.is_dir():
            raise OSError(f"Zotero storage path is not a directory: {root}")

        self.library_path = str(root)
        entries = tuple(self._scan_entries(root))
        by_name: Dict[str, List[IndexedPdf]] = {}
        by_relative: Dict[str, List[IndexedPdf]] = {}
        for entry in entries:
            by_name.setdefault(entry.normalized_filename, []).append(entry)
            by_relative.setdefault(self._normalize_relative(entry.relative_path), []).append(entry)

        self._entries = entries
        self._by_name = MappingProxyType(
            {key: tuple(value) for key, value in sorted(by_name.items())}
        )
        self._by_relative = MappingProxyType(
            {key: tuple(value) for key, value in sorted(by_relative.items())}
        )

        # Compatibility projections retained for one migration cycle. Duplicate
        # basenames are never discarded internally.
        self.file_index = MappingProxyType(
            {key: values[0].path for key, values in self._by_name.items()}
        )
        self.original_names = MappingProxyType(
            {key: values[0].filename for key, values in self._by_name.items()}
        )
        logger.info(
            "Built read-only PDF index for %s: %s files, %s unique basenames",
            self.library_path,
            self.entry_count,
            len(self),
        )

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        return unicodedata.normalize("NFC", str(filename or "")).casefold()

    @classmethod
    def _normalize_relative(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/")
        return normalized.strip("/").casefold()

    @classmethod
    def _scan_entries(cls, root: Path) -> Iterable[IndexedPdf]:
        try:
            root_entries = sorted(root.iterdir(), key=lambda path: path.name.casefold())
        except OSError as exc:
            raise OSError(f"Cannot read Zotero storage directory: {root}: {exc}") from exc

        candidates: List[Path] = []
        for entry in root_entries:
            if entry.is_symlink():
                continue
            if entry.is_file() and entry.suffix.casefold() == ".pdf":
                candidates.append(entry)
                continue
            if not entry.is_dir():
                continue
            try:
                children = sorted(entry.iterdir(), key=lambda path: path.name.casefold())
            except OSError as exc:
                logger.warning("Cannot read Zotero storage child %s: %s", entry, exc)
                continue
            candidates.extend(
                child
                for child in children
                if not child.is_symlink()
                and child.is_file()
                and child.suffix.casefold() == ".pdf"
            )

        for path in sorted(candidates, key=lambda item: cls._normalize_relative(str(item.relative_to(root)))):
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = 0
            yield IndexedPdf(
                path=str(path.resolve()),
                relative_path=str(path.relative_to(root)),
                filename=path.name,
                normalized_filename=cls._normalize_filename(path.name),
                size_bytes=size_bytes,
            )

    def __len__(self) -> int:
        """Return the legacy count of unique normalized basenames."""

        return len(self._by_name)

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> Tuple[IndexedPdf, ...]:
        return self._entries

    def find_exact_all(self, filename: str) -> Tuple[IndexedPdf, ...]:
        if not filename:
            return ()
        relative = self._normalize_relative(filename)
        relative_matches: List[IndexedPdf] = []
        if "/" in relative:
            for indexed_relative, entries in self._by_relative.items():
                if indexed_relative == relative or relative.endswith("/" + indexed_relative):
                    relative_matches.extend(entries)
            if relative_matches:
                return tuple(sorted(set(relative_matches), key=lambda item: self._normalize_relative(item.relative_path)))
        basename = Path(relative.replace("/", os.sep)).name
        return tuple(self._by_name.get(self._normalize_filename(basename), ()))

    def find_exact(self, filename: str) -> Optional[str]:
        matches = self.find_exact_all(filename)
        if len(matches) > 1:
            logger.warning(
                "Legacy find_exact selected the first of %s deterministic candidates for %s",
                len(matches),
                filename,
            )
        return matches[0].path if matches else None

    def find_fuzzy_all(self, keywords: Sequence[str]) -> Tuple[IndexedPdf, ...]:
        normalized_keywords = [
            self._normalize_filename(keyword)
            for keyword in keywords
            if str(keyword or "").strip()
        ]
        if not normalized_keywords:
            return ()
        threshold = min(3, len(normalized_keywords))
        ranked: List[Tuple[int, str, IndexedPdf]] = []
        for entry in self._entries:
            match_count = sum(1 for keyword in normalized_keywords if keyword in entry.normalized_filename)
            if match_count >= threshold:
                ranked.append((match_count, self._normalize_relative(entry.relative_path), entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked)

    def find_fuzzy(self, keywords: List[str]) -> List[Tuple[str, str]]:
        return [(entry.filename, entry.path) for entry in self.find_fuzzy_all(keywords)]


def _is_translation(filename: str) -> bool:
    translation_keywords = ["中文翻译", "翻译版", "chinese translation", "译版"]
    filename_lower = filename.casefold()
    return any(keyword.casefold() in filename_lower for keyword in translation_keywords)


def _is_supplement(filename: str) -> bool:
    supplement_keywords = [
        "supplementary material",
        "appendix",
        "si.pdf",
        "supporting information",
        "supplement.pdf",
    ]
    filename_lower = filename.casefold()
    return any(keyword in filename_lower for keyword in supplement_keywords)


def _title_overlap(filename: str, title: str) -> float:
    title_words = {word.casefold() for word in re.findall(r"\w+", title) if len(word) > 2}
    if not title_words:
        return 0.0
    filename_words = {word.casefold() for word in re.findall(r"\w+", Path(filename).stem) if len(word) > 2}
    return len(title_words & filename_words) / len(title_words)


def _score_pdf_quality(file_path: str, filename: str, title: str = "") -> Tuple[float, str]:
    score = 50.0
    diagnostics: List[str] = []
    try:
        file_size = os.path.getsize(file_path)
        if file_size < 1024:
            score = 0.0
            diagnostics.append("file_too_small")
        elif file_size < 10 * 1024:
            score -= 5.0
            diagnostics.append("file_small")
    except OSError:
        score = 0.0
        diagnostics.append("file_unreadable")

    if _is_translation(filename):
        score -= 10.0
        diagnostics.append("possible_translation")
    if _is_supplement(filename):
        score -= 30.0
        diagnostics.append("possible_supplement")

    overlap = _title_overlap(filename, title)
    score += overlap * 40.0
    diagnostics.append(f"title_overlap={overlap:.3f}")
    return max(0.0, score), ";".join(diagnostics)


def _attachment_variants(attachment: str) -> Tuple[str, ...]:
    cleaned = str(attachment or "").strip()
    if cleaned.startswith("o "):
        cleaned = cleaned[2:].strip()
    if not cleaned:
        return ()
    variants = [cleaned]
    basename = Path(cleaned.replace("\\", os.sep).replace("/", os.sep)).name
    if basename and basename not in variants:
        variants.append(basename)
    if basename and not basename.casefold().endswith(".pdf"):
        variants.append(f"{basename}.pdf")
    return tuple(dict.fromkeys(variants))


def _candidate_for(entry: IndexedPdf, match_kind: str, title: str) -> PdfMatchCandidateV1:
    quality, quality_diagnostic = _score_pdf_quality(entry.path, entry.filename, title)
    match_score = {
        "attachment_relative": 120.0,
        "attachment_basename": 80.0,
        "title_fuzzy": 0.0,
    }[match_kind]
    overlap = _title_overlap(entry.filename, title)
    return PdfMatchCandidateV1(
        path=entry.path,
        match_kind=match_kind,  # type: ignore[arg-type]
        score=match_score + quality,
        score_components=MappingProxyType(
            {"match": match_score, "quality": quality, "title_overlap": overlap}
        ),
        diagnostics=(quality_diagnostic,),
    )


def resolve_pdf_match(
    paper_meta: Mapping[str, Any],
    library_path: str,
    file_index: Optional[FileIndex] = None,
) -> PdfMatchResultV1:
    """Return a deterministic PDF match without silently resolving ambiguity."""

    index = file_index or create_file_index(library_path)
    title = str(paper_meta.get("title") or "")
    attachments = [str(item) for item in (paper_meta.get("attachments") or []) if str(item).strip()]
    by_path: Dict[str, PdfMatchCandidateV1] = {}

    for attachment in attachments:
        variants = _attachment_variants(attachment)
        for variant in variants:
            matches = index.find_exact_all(variant)
            has_relative_hint = "/" in variant.replace("\\", "/")
            match_kind = "attachment_relative" if has_relative_hint else "attachment_basename"
            for entry in matches:
                candidate = _candidate_for(entry, match_kind, title)
                existing = by_path.get(entry.path)
                if existing is None or candidate.score > existing.score:
                    by_path[entry.path] = candidate

    if not by_path and title:
        keywords = [word for word in re.findall(r"\w+", title) if len(word) > 3][:10]
        for entry in index.find_fuzzy_all(keywords):
            by_path[entry.path] = _candidate_for(entry, "title_fuzzy", title)

    candidates = tuple(sorted(by_path.values(), key=lambda item: (-item.score, item.path.casefold())))
    if not candidates:
        return PdfMatchResultV1(status="not_found", diagnostics=("no_pdf_candidate",))

    viable = tuple(candidate for candidate in candidates if candidate.score > 0)
    if not viable:
        return PdfMatchResultV1(
            status="not_found",
            candidates=candidates,
            diagnostics=("all_candidates_failed_quality_gate",),
        )

    best = viable[0]
    if len(viable) > 1 and best.score - viable[1].score < 5.0:
        return PdfMatchResultV1(
            status="ambiguous",
            candidates=viable,
            diagnostics=("top_candidates_within_margin",),
        )
    return PdfMatchResultV1(
        status="matched",
        selected_path=best.path,
        candidates=viable,
        diagnostics=("deterministic_match",),
    )


def find_pdf(
    paper_meta: Dict[str, Any],
    library_path: str,
    file_index: Optional[FileIndex] = None,
) -> Optional[str]:
    """Compatibility projection returning a path only for an unambiguous match."""

    result = resolve_pdf_match(paper_meta, library_path, file_index)
    if result.status == "ambiguous":
        logger.error(
            "Ambiguous PDF match for %s: %s",
            paper_meta.get("title") or "unknown",
            [candidate.path for candidate in result.candidates],
        )
    return result.selected_path or None


def create_file_index(library_path: str) -> FileIndex:
    """Build a read-only index. No write probe is performed."""

    return FileIndex(library_path)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2:
        index = create_file_index(sys.argv[1])
        result = find_pdf({"attachments": [sys.argv[2]]}, sys.argv[1], index)
        logger.info("Found file: %s" if result else "File not found", result or "")
    else:
        logger.info("Usage: python file_finder.py <library_path> <filename>")
