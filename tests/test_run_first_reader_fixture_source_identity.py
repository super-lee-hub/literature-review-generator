from pathlib import Path

import fitz
import pytest

from tests.test_pr25_repair_v2 import (
    _source_bound_four_paper_reader_response,
    _write_source_pdf,
)
from tests.test_run_first_run_envelope import _FOUR_SOURCE_ROWS


def test_reader_fixture_retains_actual_pdf_source_identity_across_order_and_repeats(tmp_path: Path):
    documents = []
    for key, title, finding in _FOUR_SOURCE_ROWS:
        path = tmp_path / f"{key}.pdf"
        _write_source_pdf(path, title=title, finding=finding)
        with fitz.open(path) as document:
            documents.append((title, finding, "\n".join(page.get_text() for page in document)))
    for title, finding, source_text in [*reversed(documents), *documents, documents[0]]:
        summary = _source_bound_four_paper_reader_response(source_text)
        assert summary["core_analysis"]["findings"] == finding
        assert title in summary["core_analysis"]["summary"]
    with pytest.raises(AssertionError, match="exactly one"):
        _source_bound_four_paper_reader_response(documents[0][2] + documents[1][2])
    with pytest.raises(AssertionError, match="exactly one"):
        _source_bound_four_paper_reader_response(documents[0][2].replace(documents[0][1], documents[1][1]))
