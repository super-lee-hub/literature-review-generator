import json
from pathlib import Path
from typing import cast

import main
from services.paper_identity import build_paper_key, normalize_doi
from services.summary_reuse import collect_summary_sources


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def success(self, *_args, **_kwargs):
        pass


def _make_summary(
    *,
    title: str,
    authors: list[str],
    year: str,
    doi: str = "",
    status: str = "success",
) -> dict:
    return {
        "paper_info": {
            "title": title,
            "authors": authors,
            "year": year,
            "journal": "Journal",
            "doi": doi,
        },
        "status": status,
        "ai_summary": {"paper_metadata": {"doi": doi}},
    }


def _make_generator(tmp_path: Path, output_root: Path, *, project_name: str = "current") -> main.LiteratureReviewGenerator:
    current_workspace = output_root / f"{project_name}__job999" / "artifacts"
    current_workspace.mkdir(parents=True, exist_ok=True)
    generator = main.LiteratureReviewGenerator(project_name=project_name, pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = {"Paths": {"output_path": str(output_root)}}
    generator.output_dir = str(output_root / f"{project_name}__job999")
    generator.project_name = project_name
    generator.summary_file = str(current_workspace / f"{project_name}_summaries.json")
    return generator


def test_normalize_doi_canonicalizes_url_prefix_and_case() -> None:
    assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert normalize_doi("DOI:10.1000/ABC.") == "10.1000/abc"


def test_apply_stage1_cross_run_reuse_uses_global_catalog_and_skips_non_matches(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "output"
    prior_workspace = output_root / "prior__job123" / "artifacts"
    prior_workspace.mkdir(parents=True)
    (prior_workspace / "prior_summaries.json").write_text(
        json.dumps(
            [
                _make_summary(
                    title="Prior Paper",
                    authors=["Alice Example"],
                    year="2024",
                    doi="https://doi.org/10.1000/demo",
                )
            ]
        ),
        encoding="utf-8",
    )

    generator = _make_generator(tmp_path, output_root)
    generator.reuse_stage1 = True
    generator.papers = [
        {
            "title": "Current Match",
            "authors": ["Alice Example"],
            "doi": "10.1000/DEMO",
            "pdf_path": str(tmp_path / "match.pdf"),
            "source_mode": "direct",
        },
        {
            "title": "Current No DOI",
            "authors": ["Alice Example"],
            "doi": "",
            "pdf_path": str(tmp_path / "no-doi.pdf"),
            "source_mode": "direct",
        },
    ]

    monkeypatch.setattr(generator, "save_summaries", lambda: True)
    monkeypatch.setattr(generator, "save_checkpoint", lambda: True)
    monkeypatch.setattr(generator, "_persist_paper_artifact", lambda _result: True)

    assert generator._apply_stage1_cross_run_reuse() is True
    assert len(generator.summaries) == 1

    reused_summary = generator.summaries[0]
    assert reused_summary["paper_info"]["doi"] == "10.1000/demo"
    assert build_paper_key(reused_summary["paper_info"]) in generator._checkpoint_processed_papers
    assert generator._stage1_reuse_report is not None
    assert len(generator._stage1_reuse_report["reused_papers"]) == 1
    assert generator._stage1_reuse_report["not_reused"][0]["reason"] == "no_matching_summary"


def test_collect_summary_sources_includes_legacy_project_outputs(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    legacy_project = output_root / "legacy_project"
    legacy_project.mkdir(parents=True)
    (legacy_project / "legacy_project_summaries.json").write_text("[]", encoding="utf-8")

    sources = collect_summary_sources(
        explicit_paths=None,
        output_root=output_root,
        current_workspace_root=None,
        current_summary_file=None,
    )

    assert any(source.source_type == "legacy_output" for source in sources)
    assert any(source.path.endswith("legacy_project_summaries.json") for source in sources)


def test_apply_stage1_cross_run_reuse_uses_multiple_historical_projects_and_non_doi_matches(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "output"
    first_workspace = output_root / "first__job111" / "artifacts"
    second_workspace = output_root / "second__job222" / "artifacts"
    first_workspace.mkdir(parents=True)
    second_workspace.mkdir(parents=True)
    (first_workspace / "first_summaries.json").write_text(
        json.dumps(
            [
                _make_summary(
                    title="Mechanism Paper",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/mechanism",
                )
            ]
        ),
        encoding="utf-8",
    )
    (second_workspace / "second_summaries.json").write_text(
        json.dumps(
            [
                _make_summary(
                    title="Overlap Without DOI",
                    authors=["Bob Example", "Carol Writer"],
                    year="2023",
                )
            ]
        ),
        encoding="utf-8",
    )

    generator = _make_generator(tmp_path, output_root, project_name="third")
    generator.reuse_stage1 = True
    generator.papers = [
        {
            "title": "Mechanism Paper",
            "authors": ["Alice Example"],
            "year": "2024",
            "doi": "10.1000/mechanism",
        },
        {
            "title": "Overlap Without DOI",
            "authors": ["Bob Example", "Carol Writer"],
            "year": "2023",
            "doi": "",
        },
        {
            "title": "Brand New Paper",
            "authors": ["Dana Researcher"],
            "year": "2022",
            "doi": "",
        },
    ]

    monkeypatch.setattr(generator, "save_summaries", lambda: True)
    monkeypatch.setattr(generator, "save_checkpoint", lambda: True)
    monkeypatch.setattr(generator, "_persist_paper_artifact", lambda _result: True)

    assert generator._apply_stage1_cross_run_reuse() is True
    assert len(generator.summaries) == 2
    assert {item["match_type"] for item in generator._stage1_reuse_report["reused_papers"]} == {
        "doi_exact",
        "canonical_paper_key_exact",
    }
    winner_paths = {item["winner_source"]["path"] for item in generator._stage1_reuse_report["reused_papers"]}
    assert any(path.endswith("first_summaries.json") for path in winner_paths)
    assert any(path.endswith("second_summaries.json") for path in winner_paths)
    assert generator._stage1_reuse_report["preview"]["needs_analysis_count"] == 1


def test_apply_stage1_cross_run_reuse_marks_ambiguous_title_author_year_candidates(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "output"
    first_workspace = output_root / "first__job111" / "artifacts"
    second_workspace = output_root / "second__job222" / "artifacts"
    first_workspace.mkdir(parents=True)
    second_workspace.mkdir(parents=True)
    (first_workspace / "first_summaries.json").write_text(
        json.dumps(
            [
                _make_summary(
                    title="Ambiguous Paper",
                    authors=["Alice Example", "Bob Writer"],
                    year="2024",
                )
            ]
        ),
        encoding="utf-8",
    )
    (second_workspace / "second_summaries.json").write_text(
        json.dumps(
            [
                _make_summary(
                    title="Ambiguous Paper",
                    authors=["Alice Example", "Carol Analyst"],
                    year="2024",
                )
            ]
        ),
        encoding="utf-8",
    )

    generator = _make_generator(tmp_path, output_root, project_name="ambiguous")
    generator.reuse_stage1 = True
    generator.papers = [
        {
            "title": "Ambiguous Paper",
            "authors": ["Alice Example"],
            "year": "2024",
            "doi": "",
        }
    ]

    monkeypatch.setattr(generator, "save_summaries", lambda: True)
    monkeypatch.setattr(generator, "save_checkpoint", lambda: True)
    monkeypatch.setattr(generator, "_persist_paper_artifact", lambda _result: True)

    assert generator._apply_stage1_cross_run_reuse() is True
    assert generator.summaries == []
    assert generator._stage1_reuse_report["not_reused"][0]["reason"] == "ambiguous_match"
    assert len(generator._stage1_reuse_report["not_reused"][0]["ambiguous_candidates"]) == 2


def test_process_all_papers_only_processes_missing_papers_after_reuse(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "output"
    prior_workspace = output_root / "prior__job123" / "artifacts"
    prior_workspace.mkdir(parents=True)
    (prior_workspace / "prior_summaries.json").write_text(
        json.dumps(
            [
                _make_summary(
                    title="Reusable Paper",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/reused",
                )
            ]
        ),
        encoding="utf-8",
    )

    generator = _make_generator(tmp_path, output_root, project_name="incremental")
    generator.reuse_stage1 = True
    generator.config = {
        "Paths": {"output_path": str(output_root)},
        "Performance": {"max_workers": "1"},
    }
    generator.papers = [
        {
            "title": "Reusable Paper",
            "authors": ["Alice Example"],
            "year": "2024",
            "doi": "10.1000/reused",
        },
        {
            "title": "Needs Analysis",
            "authors": ["Dana Researcher"],
            "year": "2022",
            "doi": "",
        },
    ]

    processed_titles: list[str] = []

    def _process_paper(paper, *_args, **_kwargs):
        processed_titles.append(str(paper.get("title") or ""))
        return {
            "paper_info": dict(paper),
            "status": "success",
        }

    monkeypatch.setattr(generator, "save_summaries", lambda: True)
    monkeypatch.setattr(generator, "save_checkpoint", lambda: True)
    monkeypatch.setattr(generator, "_persist_paper_artifact", lambda _result: True)
    monkeypatch.setattr(generator, "process_paper", _process_paper)

    assert generator._apply_stage1_cross_run_reuse() is True
    assert generator.process_all_papers() is True
    assert processed_titles == ["Needs Analysis"]


def test_load_existing_summaries_materializes_summary_file_override(tmp_path: Path) -> None:
    external_summary = tmp_path / "subset.json"
    external_summary.write_text(
        json.dumps(
            [
                {
                    "paper_info": {"title": "Subset Paper", "doi": "10.1000/subset"},
                    "status": "success",
                    "ai_summary": {"paper_metadata": {"doi": "10.1000/subset"}},
                }
            ]
        ),
        encoding="utf-8",
    )

    generator = main.LiteratureReviewGenerator(project_name="subset", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.project_name = "subset"
    generator.output_dir = str(tmp_path / "workspace")
    generator.summary_file = str(tmp_path / "workspace" / "artifacts" / "subset_summaries.json")
    generator.summary_file_override = str(external_summary)

    assert generator.load_existing_summaries() is True
    assert generator.summaries[0]["paper_info"]["title"] == "Subset Paper"
    assert json.loads(Path(generator.summary_file).read_text(encoding="utf-8"))[0]["paper_info"]["doi"] == "10.1000/subset"
    manifest_path = Path(generator._get_summary_source_manifest_path())
    assert manifest_path.exists()
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["source_kind"] == "explicit_summary_file"
    assert manifest_payload["source_path"] == str(external_summary.resolve())


def test_load_existing_summaries_materializes_multiple_sources(tmp_path: Path) -> None:
    first_summary = tmp_path / "subset-a.json"
    second_summary = tmp_path / "subset-b.json"
    first_summary.write_text(
        json.dumps(
            [
                _make_summary(
                    title="Subset Paper A",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/subset-a",
                )
            ]
        ),
        encoding="utf-8",
    )
    second_summary.write_text(
        json.dumps(
            [
                _make_summary(
                    title="Subset Paper B",
                    authors=["Bob Example"],
                    year="2023",
                    doi="10.1000/subset-b",
                )
            ]
        ),
        encoding="utf-8",
    )

    generator = main.LiteratureReviewGenerator(project_name="subset", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.project_name = "subset"
    generator.output_dir = str(tmp_path / "workspace")
    generator.summary_file = str(tmp_path / "workspace" / "artifacts" / "subset_summaries.json")
    generator.summary_file_override = str(first_summary)
    generator.summary_source_overrides = [str(second_summary)]

    assert generator.load_existing_summaries() is True
    assert len(generator.summaries) == 2
    titles = {item["paper_info"]["title"] for item in generator.summaries}
    assert titles == {"Subset Paper A", "Subset Paper B"}

    manifest_path = Path(generator._get_summary_source_manifest_path())
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["source_kind"] == "explicit_summary_sources"
    assert len(manifest_payload["source_items"]) == 2
