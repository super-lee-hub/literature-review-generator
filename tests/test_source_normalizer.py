import pytest

from services.source_normalizer import normalize_source_papers


def test_source_normalizer_aligns_direct_pdf_inputs(tmp_path) -> None:
    pdf_path = tmp_path / "paper-a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 demo")

    papers = [
        {
            "title": "Paper A",
            "authors": ["Alice Example"],
            "doi": "",
            "pdf_path": str(pdf_path),
        }
    ]

    descriptors = normalize_source_papers("direct", papers)
    assert descriptors[0].source_mode == "direct"
    assert descriptors[0].source_pdf == str(pdf_path)
    assert descriptors[0].source_pdf_fingerprint


def test_source_normalizer_aligns_zotero_inputs() -> None:
    papers = [
        {
            "title": "Paper B",
            "authors": ["Bob Example"],
            "doi": "10.1000/demo",
            "pdf_path": "D:/library/paper-b.pdf",
        }
    ]

    descriptors = normalize_source_papers("zotero", papers)

    assert descriptors[0].source_mode == "zotero"
    assert descriptors[0].source_paper_id == "10.1000/demo"
    assert descriptors[0].canonical_paper_key == "10.1000/demo"
    assert "10.1000/demo" in descriptors[0].paper_key_aliases
    assert descriptors[0].metadata_confidence == "high"


def test_source_normalizer_rejects_duplicate_canonical_paper_keys(tmp_path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"%PDF-1.4 first")
    second.write_bytes(b"%PDF-1.4 second")

    with pytest.raises(ValueError, match="source_identity_duplicate_canonical_paper_key"):
        normalize_source_papers(
            "direct",
            [
                {"title": "First", "doi": "10.1000/same", "pdf_path": str(first)},
                {"title": "Second", "doi": "10.1000/same", "pdf_path": str(second)},
            ],
        )


def test_source_normalizer_rejects_same_pdf_under_different_filenames(tmp_path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "renamed-copy.pdf"
    first.write_bytes(b"%PDF-1.4 identical")
    second.write_bytes(first.read_bytes())

    with pytest.raises(ValueError, match="source_identity_duplicate_pdf_sha256"):
        normalize_source_papers(
            "direct",
            [
                {"title": "First", "pdf_path": str(first)},
                {"title": "Second", "pdf_path": str(second)},
            ],
        )
