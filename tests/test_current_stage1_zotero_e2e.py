from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import fitz  # type: ignore

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.runner import AgentRuntimeRunner
from tests.test_current_runtime_full_e2e import _reader_summary, _test_config


def _write_zotero_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Zotero Study\n"
        "Example Author\n"
        "2025\n"
        "10.1000/zotero\n"
        "Methodology: A controlled empirical study.\n"
        "Results: The treatment improved the outcome.\n",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()


def test_current_zotero_stage1_runs_through_runtime_and_persists_identity(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    library = tmp_path / "zotero-storage"
    pdf_path = library / "ABCD1234" / "zotero.pdf"
    _write_zotero_pdf(pdf_path)
    report_path = tmp_path / "zotero-report.txt"
    report_path.write_text(
        "*\n"
        "Item Type: Journal Article\n"
        "Author: Example Author\n"
        "Title: Zotero Study\n"
        "Publication: Example Journal\n"
        "Year: 2025\n"
        "DOI: 10.1000/zotero\n"
        "Attachment: zotero.pdf\n",
        encoding="utf-8",
    )

    def configured_reader(
        _service: Any,
        *,
        item: Any,
        built_input: Any,
        primary_config: Mapping[str, Any],
        backup_config: Mapping[str, Any],
        runtime: Any,
    ) -> Mapping[str, Any]:
        del built_input, primary_config, backup_config, runtime
        paper_info = dict(item.paper_info)
        summary = _reader_summary(
            str(paper_info["canonical_paper_key"]),
            str(paper_info["title"]),
            "The treatment improved the outcome.",
        )
        summary["paper_info"].update(
            {
                "canonical_paper_key": paper_info["canonical_paper_key"],
                "source_paper_id": paper_info["source_paper_id"],
                "authors": paper_info.get("authors") or ["Example Author"],
                "year": paper_info.get("year") or 2025,
            }
        )
        return {"status": "success", "content": summary}

    monkeypatch.setattr(
        "services.stage1_analysis_service.Stage1AnalysisService._call_reader",
        configured_reader,
    )
    spec = RuntimeJobSpec(
        project_name="current-zotero-stage1",
        source=RuntimeSourceSpec(
            mode="zotero",
            zotero_report=str(report_path),
            library_path=str(library),
        ),
        job_id="current-zotero-stage1-job",
        config=str(_test_config(tmp_path)),
        action="analyze",
        queue_file=str(tmp_path / "queue.json"),
        metadata={"requested_stages": ["analyze"]},
    )

    result = AgentRuntimeRunner(spec).run()

    assert result.job_status == "completed", result
    assert result.completed_stages == ("source_intake", "analyze"), result
    registry = json.loads(
        (Path(result.workspace_path) / "artifact_registry.json").read_text(encoding="utf-8")
    )
    source_bundle_record = next(
        item for item in registry["artifacts"] if item["artifact_id"] == "source_bundle"
    )
    source_bundle = json.loads(Path(source_bundle_record["path"]).read_text(encoding="utf-8"))
    assert source_bundle["source_mode"] == "zotero"
    assert source_bundle["paper_work_items"][0]["source_pdf"] == str(pdf_path.resolve())
    assert source_bundle["source_snapshot"]["canonical_ready"] is True
    assert any(item["artifact_id"] == "stage1_provider_receipts" for item in registry["artifacts"])
