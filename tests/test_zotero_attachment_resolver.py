from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import fitz  # type: ignore

from runtime.source_intake import (
    _inspect_pdf_candidate,
    _select_identity_candidate,
    build_zotero_source_bundle,
)
from runtime.zotero_attachment_resolver import ZoteroAttachmentIndex


def _create_zotero_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    zotero_root = tmp_path / "Zotero"
    storage = zotero_root / "storage"
    storage.mkdir(parents=True)
    linked = tmp_path / "linked" / "paper.pdf"
    linked.parent.mkdir()
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A Relation Paper\nAlice Smith\n2024")
    document.save(linked)
    document.close()

    database = zotero_root / "zotero.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY,
            itemTypeID INTEGER,
            dateAdded TEXT,
            dateModified TEXT,
            clientDateModified TEXT,
            libraryID INTEGER,
            key TEXT,
            version INTEGER,
            synced INTEGER
        );
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE creators (
            creatorID INTEGER PRIMARY KEY,
            firstName TEXT,
            lastName TEXT,
            fieldMode INTEGER
        );
        CREATE TABLE itemCreators (
            itemID INTEGER,
            creatorID INTEGER,
            creatorTypeID INTEGER,
            orderIndex INTEGER
        );
        CREATE TABLE itemAttachments (
            itemID INTEGER,
            parentItemID INTEGER,
            linkMode INTEGER,
            contentType TEXT,
            charsetID INTEGER,
            path TEXT,
            syncState INTEGER,
            storageModTime TEXT,
            storageHash TEXT,
            lastProcessedModificationTime TEXT,
            lastRead TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO fields(fieldID, fieldName) VALUES (?, ?)",
        [(1, "title"), (6, "date"), (59, "DOI")],
    )
    connection.execute("INSERT INTO itemTypes(itemTypeID, typeName) VALUES (1, 'journalArticle')")
    connection.execute(
        "INSERT INTO items(itemID, itemTypeID, key) VALUES (100, 1, 'PARENT01')"
    )
    connection.execute(
        "INSERT INTO items(itemID, itemTypeID, key) VALUES (101, 1, 'ATTACH01')"
    )
    connection.executemany(
        "INSERT INTO itemDataValues(valueID, value) VALUES (?, ?)",
        [
            (1, "A Relation Paper"),
            (2, "2024-00-00 2024"),
            (3, "10.1234/relation"),
        ],
    )
    connection.executemany(
        "INSERT INTO itemData(itemID, fieldID, valueID) VALUES (?, ?, ?)",
        [(100, 1, 1), (100, 6, 2), (100, 59, 3)],
    )
    connection.execute(
        "INSERT INTO creators(creatorID, firstName, lastName, fieldMode) VALUES (1, 'Alice', 'Smith', 0)"
    )
    connection.execute(
        "INSERT INTO itemCreators(itemID, creatorID, creatorTypeID, orderIndex) VALUES (100, 1, 1, 0)"
    )
    connection.execute(
        "INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path) "
        "VALUES (101, 100, 2, 'application/pdf', ?)" ,
        (str(linked),),
    )
    connection.commit()
    connection.close()
    return storage, database, linked


def test_zotero_attachment_index_reads_parent_relation_read_only(tmp_path: Path) -> None:
    storage, _database, linked = _create_zotero_fixture(tmp_path)

    index = ZoteroAttachmentIndex(storage)
    resolution = index.resolve(
        {
            "title": "A Relation Paper",
            "authors": ["Alice Smith"],
            "year": "2024",
            "doi": "10.1234/relation",
        }
    )

    assert index.database_access_mode == "live_read_only"
    assert index.database_integrity == "ok"
    assert resolution["match_method"] == "doi"
    assert resolution["parent_count"] == 1
    assert resolution["attachments"][0]["resolved_path"] == str(linked.resolve())
    assert resolution["attachments"][0]["link_mode"] == 2


def test_source_intake_uses_verified_zotero_relation_for_external_attachment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage, _database, linked = _create_zotero_fixture(tmp_path)
    report = tmp_path / "report.txt"
    report.write_text(
        "*\nA Relation Paper\n作者\tAlice Smith\n日期\t2024\n"
        "DOI\t10.1234/relation\n附件\n\n  o PDF\n",
        encoding="utf-8",
    )

    class _Identity:
        identity_verdict = "match"
        artifact_status = "ready"
        canonical_ready = True
        reasons = ("test_identity_match",)
        diagnostics = ()

        def to_dict(self):
            return {
                "identity_verdict": self.identity_verdict,
                "artifact_status": self.artifact_status,
                "canonical_ready": self.canonical_ready,
                "reasons": list(self.reasons),
                "diagnostics": list(self.diagnostics),
            }

    monkeypatch.setattr("runtime.source_intake.inspect_pdf_identity", lambda _paper, _path: _Identity())

    bundle = build_zotero_source_bundle(
        project_name="relation-test",
        zotero_report=str(report),
        library_path=str(storage),
    )

    assert len(bundle.paper_work_items) == 1
    assert bundle.paper_work_items[0].source_pdf == str(linked.resolve())
    assert bundle.source_snapshot["canonical_ready"] is True
    assert bundle.source_snapshot["missing_titles"] == []
    assert bundle.source_snapshot["ambiguous_matches"] == []
    assert bundle.source_snapshot["zotero_database"]["access_mode"] == "live_read_only"


def test_identity_selection_keeps_different_hash_matches_ambiguous() -> None:
    status, reason, selected = _select_identity_candidate(
        [
            {"path": "b.pdf", "canonical_ready": True, "sha256": "b"},
            {"path": "a.pdf", "canonical_ready": True, "sha256": "a"},
        ],
        resolved_source="zotero_db_attachment",
    )

    assert status == "ambiguous"
    assert reason == "multiple_identity_matches_with_different_hashes"
    assert selected is None


def test_identity_selection_uses_deterministic_path_for_identical_duplicates() -> None:
    status, reason, selected = _select_identity_candidate(
        [
            {"path": "b.pdf", "canonical_ready": True, "sha256": "same"},
            {"path": "a.pdf", "canonical_ready": True, "sha256": "same"},
        ],
        resolved_source="zotero_db_attachment",
    )

    assert status == "matched"
    assert reason == "duplicate_identical_candidates"
    assert selected is not None
    assert selected["path"] == "a.pdf"


def test_identity_cache_is_bound_to_paper_identity(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "shared.pdf"
    pdf.write_bytes(b"%PDF-1.4\nshared\n")
    calls: list[str] = []

    def fake_inspect(paper, _path):
        calls.append(str(paper["title"]))
        matched = paper["title"] == "Paper A"
        return SimpleNamespace(
            identity_verdict="match" if matched else "mismatch",
            artifact_status="ready" if matched else "quarantined",
            canonical_ready=matched,
            reasons=("test",),
            diagnostics=(),
        )

    monkeypatch.setattr("runtime.source_intake.inspect_pdf_identity", fake_inspect)
    identities = {}
    hashes = {}
    first = _inspect_pdf_candidate(
        {"title": "Paper A", "authors": ["Alice Smith"], "year": "2024"},
        str(pdf),
        source="test",
        identity_cache=identities,
        hash_cache=hashes,
    )
    second = _inspect_pdf_candidate(
        {"title": "Paper B", "authors": ["Bob Jones"], "year": "2024"},
        str(pdf),
        source="test",
        identity_cache=identities,
        hash_cache=hashes,
    )

    assert first["canonical_ready"] is True
    assert second["canonical_ready"] is False
    assert calls == ["Paper A", "Paper B"]
