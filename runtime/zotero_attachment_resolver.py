"""Read-only Zotero parent/attachment resolution for source intake.

Zotero reports expose an attachment display title, which is not necessarily the
physical filename in the storage tree.  This module reads the local Zotero
database without mutating it and returns only relationship candidates.  The
caller remains responsible for PDF identity verification and fail-closed
selection.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from services.paper_identity import (
    normalize_doi,
    normalized_author_surnames,
    normalized_title_key,
)


@dataclass(frozen=True)
class ZoteroAttachmentRecord:
    item_id: int
    parent_item_id: int
    attachment_key: str
    link_mode: int
    content_type: str
    raw_path: str
    resolved_path: str
    exists: bool
    date_added: str = ""
    attachment_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ZoteroParentRecord:
    item_id: int
    key: str
    item_type: str
    title: str
    doi: str
    date: str
    authors: tuple[str, ...]
    attachments: tuple[ZoteroAttachmentRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["authors"] = list(self.authors)
        payload["attachments"] = [item.to_dict() for item in self.attachments]
        return payload


class ZoteroAttachmentIndex:
    """Build an immutable in-memory index from a local Zotero database."""

    def __init__(self, library_path: str | Path) -> None:
        self.storage_root = Path(library_path).expanduser().resolve()
        self.zotero_root = self.storage_root.parent
        self.database_path = ""
        self.database_access_mode = "unavailable"
        self.database_integrity = ""
        self.database_diagnostics: list[str] = []
        self.journal_present = (self.zotero_root / "zotero.sqlite-journal").is_file()
        self._by_doi: dict[str, list[ZoteroParentRecord]] = defaultdict(list)
        self._by_title: dict[str, list[ZoteroParentRecord]] = defaultdict(list)
        self._load()

    @staticmethod
    def _sqlite_uri(path: Path, query: str) -> str:
        return "file:" + path.as_posix() + "?" + query

    @staticmethod
    def _collapse_windows_separators(value: str) -> str:
        text = str(value or "").strip()
        if len(text) >= 3 and text[1:3] == ":\\":
            text = re.sub(r"\\{2,}", r"\\", text)
        return text

    def _resolve_attachment_path(self, raw_path: str, attachment_key: str) -> Path:
        raw = self._collapse_windows_separators(raw_path)
        lowered = raw.casefold()
        if lowered.startswith("storage:"):
            relative = raw.split(":", 1)[1].lstrip("\\/")
            return self.storage_root / attachment_key / relative
        if lowered.startswith("attachments:"):
            relative = raw.split(":", 1)[1].lstrip("\\/")
            return self.storage_root / attachment_key / relative
        if lowered.startswith("file://"):
            raw = re.sub(r"^file://", "", raw, flags=re.IGNORECASE)
        path = Path(raw)
        return path if path.is_absolute() else self.zotero_root / raw

    def _database_candidates(self) -> tuple[tuple[Path, str], ...]:
        live = self.zotero_root / "zotero.sqlite"
        backup = self.zotero_root / "zotero.sqlite.bak"
        return (
            (live, "live_read_only"),
            (live, "live_immutable"),
            (backup, "backup_read_only"),
            (backup, "backup_immutable"),
        )

    def _load(self) -> None:
        for path, mode in self._database_candidates():
            if not path.is_file():
                continue
            query = "mode=ro" if mode.endswith("read_only") else "immutable=1"
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    self._sqlite_uri(path, query),
                    uri=True,
                    timeout=1,
                )
                integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"quick_check={integrity}")
                self._load_connection(connection)
                self.database_path = str(path.resolve())
                self.database_access_mode = mode
                self.database_integrity = integrity
                return
            except Exception as exc:
                self.database_diagnostics.append(
                    f"{path}:{mode}:{type(exc).__name__}:{exc}"
                )
            finally:
                if connection is not None:
                    connection.close()

    def _load_connection(self, connection: sqlite3.Connection) -> None:
        fields = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT fieldID, fieldName FROM fields")
        }
        values: dict[int, dict[str, str]] = defaultdict(dict)
        for item_id, field_id, value in connection.execute(
            "SELECT d.itemID, d.fieldID, v.value "
            "FROM itemData d JOIN itemDataValues v ON v.valueID=d.valueID"
        ):
            field_name = fields.get(int(field_id), "")
            if field_name in {"title", "DOI", "date"}:
                values[int(item_id)][field_name] = str(value or "")

        creators: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for item_id, first_name, last_name, order_index in connection.execute(
            "SELECT ic.itemID, c.firstName, c.lastName, ic.orderIndex "
            "FROM itemCreators ic JOIN creators c ON c.creatorID=ic.creatorID "
            "ORDER BY ic.itemID, ic.orderIndex"
        ):
            display = " ".join(
                part
                for part in (str(first_name or "").strip(), str(last_name or "").strip())
                if part
            )
            if display:
                creators[int(item_id)].append((int(order_index), display))

        item_types = {
            int(row[0]): str(row[1])
            for row in connection.execute("SELECT itemTypeID, typeName FROM itemTypes")
        }
        items: dict[int, tuple[str, str, str]] = {
            int(item_id): (
                str(key or ""),
                item_types.get(int(type_id), ""),
                str(date_added or ""),
            )
            for item_id, type_id, key, date_added in connection.execute(
                "SELECT itemID, itemTypeID, key, dateAdded FROM items"
            )
        }
        attachment_item_ids = {
            int(row[0]) for row in connection.execute("SELECT itemID FROM itemAttachments")
        }
        attachments_by_parent: dict[int, list[ZoteroAttachmentRecord]] = defaultdict(list)
        for item_id, parent_id, attachment_key, attachment_date_added, link_mode, content_type, raw_path in connection.execute(
            "SELECT a.itemID, a.parentItemID, ai.key, ai.dateAdded, a.linkMode, a.contentType, a.path "
            "FROM itemAttachments a JOIN items ai ON ai.itemID=a.itemID"
        ):
            raw = str(raw_path or "")
            resolved = self._resolve_attachment_path(raw, str(attachment_key or ""))
            exists = resolved.is_file()
            normalized_path = str(resolved.resolve()) if exists else str(resolved)
            attachments_by_parent[int(parent_id)].append(
                ZoteroAttachmentRecord(
                    item_id=int(item_id),
                    parent_item_id=int(parent_id),
                    attachment_key=str(attachment_key or ""),
                    link_mode=int(link_mode or 0),
                    content_type=str(content_type or ""),
                    raw_path=raw,
                    resolved_path=normalized_path,
                    exists=exists,
                    date_added=str(attachment_date_added or ""),
                    attachment_title=str(values.get(int(item_id), {}).get("title") or ""),
                )
            )

        for item_id, (key, item_type, _date_added) in items.items():
            if item_id in attachment_item_ids:
                continue
            data = values.get(item_id, {})
            title = str(data.get("title") or "")
            doi = normalize_doi(data.get("DOI"))
            if not title and not doi:
                continue
            parent = ZoteroParentRecord(
                item_id=item_id,
                key=key,
                item_type=item_type,
                title=title,
                doi=doi,
                date=str(data.get("date") or ""),
                authors=tuple(
                    name
                    for _order, name in sorted(creators.get(item_id, []))
                ),
                attachments=tuple(attachments_by_parent.get(item_id, [])),
            )
            if doi:
                self._by_doi[doi].append(parent)
            title_key = normalized_title_key(title)
            if title_key and title_key != "unknown_title":
                self._by_title[title_key].append(parent)

    @staticmethod
    def _safe_year(value: Any) -> str:
        match = re.search(r"(?:19|20)\d{2}", str(value or ""))
        return match.group(0) if match else ""

    @staticmethod
    def _paper_authors(paper: Mapping[str, Any]) -> list[str]:
        raw = paper.get("authors")
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        return [str(item) for item in (raw or []) if str(item).strip()]

    def match_parents(self, paper: Mapping[str, Any]) -> tuple[str, tuple[ZoteroParentRecord, ...]]:
        doi = normalize_doi(paper.get("doi"))
        if doi and self._by_doi.get(doi):
            return "doi", tuple(self._by_doi[doi])

        title_key = normalized_title_key(paper.get("title"))
        candidates = list(self._by_title.get(title_key, [])) if title_key else []
        if len(candidates) > 1:
            year = self._safe_year(paper.get("year") or paper.get("date"))
            if year:
                filtered = [item for item in candidates if year in self._safe_year(item.date)]
                if filtered:
                    candidates = filtered
            expected_surnames = normalized_author_surnames(self._paper_authors(paper))
            if expected_surnames:
                filtered = [
                    item
                    for item in candidates
                    if normalized_author_surnames(item.authors)[:1] == expected_surnames[:1]
                ]
                if filtered:
                    candidates = filtered
        return ("title_exact" if candidates else "none"), tuple(candidates)

    def resolve(self, paper: Mapping[str, Any]) -> dict[str, Any]:
        method, parents = self.match_parents(paper)
        attachments = [
            attachment
            for parent in parents
            for attachment in parent.attachments
            if attachment.content_type.casefold() == "application/pdf"
            or attachment.resolved_path.casefold().endswith(".pdf")
        ]
        return {
            "database_path": self.database_path,
            "database_access_mode": self.database_access_mode,
            "database_integrity": self.database_integrity,
            "journal_present": self.journal_present,
            "database_diagnostics": list(self.database_diagnostics),
            "match_method": method,
            "parent_count": len(parents),
            "parents": [parent.to_dict() for parent in parents],
            "attachments": [attachment.to_dict() for attachment in attachments],
        }


__all__ = [
    "ZoteroAttachmentIndex",
    "ZoteroAttachmentRecord",
    "ZoteroParentRecord",
]
