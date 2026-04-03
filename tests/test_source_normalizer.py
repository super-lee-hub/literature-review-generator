from services.source_normalizer import normalize_source_papers, project_descriptors_to_legacy_papers


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
    projected = project_descriptors_to_legacy_papers(papers, descriptors)

    assert descriptors[0].source_mode == "direct"
    assert descriptors[0].source_pdf == str(pdf_path)
    assert descriptors[0].source_pdf_fingerprint
    assert projected[0].get("source_mode") == "direct"
    assert projected[0].get("source_descriptor", {}).get("source_pdf") == str(pdf_path)


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
