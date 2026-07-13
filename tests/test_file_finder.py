from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from file_finder import FileIndex, PdfMatchResultV1, create_file_index, resolve_pdf_match


def _pdf(path: Path, marker: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + marker * 2048)
    return path


def test_file_indexes_are_isolated_by_library_root(tmp_path: Path) -> None:
    root_a = tmp_path / "library-a"
    root_b = tmp_path / "library-b"
    path_a = _pdf(root_a / "KEYA" / "paper.pdf", b"a")
    path_b = _pdf(root_b / "KEYB" / "paper.pdf", b"b")

    index_a = FileIndex(str(root_a))
    index_b = FileIndex(str(root_b))

    assert index_a is not index_b
    assert index_a.library_path != index_b.library_path
    assert index_a.find_exact("paper.pdf") == str(path_a.resolve())
    assert index_b.find_exact("paper.pdf") == str(path_b.resolve())


def test_duplicate_basename_preserves_all_candidates_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "library"
    first = _pdf(root / "AAAA" / "paper.pdf", b"a")
    second = _pdf(root / "BBBB" / "paper.pdf", b"b")

    index = FileIndex(str(root))
    matches = index.find_exact_all("paper.pdf")

    assert index.entry_count == 2
    assert len(index) == 1
    assert [entry.path for entry in matches] == [str(first.resolve()), str(second.resolve())]


def test_create_file_index_is_read_only_and_includes_root_and_child_pdfs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root_pdf = _pdf(root / "root.pdf", b"r")
    child_pdf = _pdf(root / "KEY" / "child.pdf", b"c")
    original_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+")
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    index = create_file_index(str(root))

    assert {entry.path for entry in index.entries} == {
        str(root_pdf.resolve()),
        str(child_pdf.resolve()),
    }
    assert not (root / ".access_test").exists()


def test_resolve_pdf_match_reports_duplicate_basename_as_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _pdf(root / "AAAA" / "paper.pdf", b"a")
    _pdf(root / "BBBB" / "paper.pdf", b"b")
    index = FileIndex(str(root))

    result = resolve_pdf_match(
        {"title": "Paper", "attachments": ["paper.pdf"]},
        str(root),
        index,
    )

    assert result.status == "ambiguous"
    assert result.selected_path == ""
    assert len(result.candidates) == 2
    assert PdfMatchResultV1.from_dict(result.to_dict()).to_dict() == result.to_dict()


def test_resolve_pdf_match_uses_attachment_relative_path_to_disambiguate(tmp_path: Path) -> None:
    root = tmp_path / "library"
    selected = _pdf(root / "AAAA" / "paper.pdf", b"a")
    _pdf(root / "BBBB" / "paper.pdf", b"b")
    index = FileIndex(str(root))

    result = resolve_pdf_match(
        {"title": "Paper", "attachments": ["AAAA/paper.pdf"]},
        str(root),
        index,
    )

    assert result.status == "matched"
    assert result.selected_path == str(selected.resolve())
