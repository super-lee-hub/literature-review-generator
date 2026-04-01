from pathlib import Path
from types import SimpleNamespace

from report_generator import generate_retry_zotero_report
from zotero_parser import parse_zotero_report


class _DummyLogger:
    def info(self, _message: str) -> None:
        pass

    def success(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


def test_generate_retry_zotero_report_round_trips_with_parser(tmp_path: Path) -> None:
    generator = SimpleNamespace(
        output_dir=str(tmp_path),
        project_name="demo",
        logger=_DummyLogger(),
        failed_papers=[
            {
                "paper_info": {
                    "title": "A Study on Testing",
                    "authors": ["Smith, John", "Doe, Jane"],
                    "year": "2023",
                    "journal": "Testing Journal",
                    "doi": "10.1234/test",
                },
                "failure_reason": "主引擎调用失败",
            },
            {
                "paper_info": {
                    "title": "Second Paper",
                    "authors": ["Wang, Wu"],
                    "year": "2024",
                    "journal": "Journal B",
                    "doi": "",
                },
                "failure_reason": "PDF 文本提取失败",
            },
        ],
    )

    assert generate_retry_zotero_report(generator) is True

    report_path = tmp_path / "demo_zotero_report_for_retry.txt"
    parsed = parse_zotero_report(str(report_path))

    assert [paper.get("title") for paper in parsed] == ["A Study on Testing", "Second Paper"]
    assert parsed[0].get("authors") == ["Smith, John", "Doe, Jane"]
    assert parsed[1].get("authors") == ["Wang, Wu"]
    assert "issue" not in parsed[0]
