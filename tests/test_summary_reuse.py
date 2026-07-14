import json
from pathlib import Path
from typing import cast

import pytest

import main
from config_loader import ConfigDict
from runtime.reconcile import RuntimeReconciler
from services.paper_identity import build_paper_key, normalize_doi
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.audit_record import AuditRecordV1
from services.job_workspace import JobWorkspace
from services.summary_reuse import (
    SummarySource,
    SummarySourceError,
    collect_summary_sources,
    index_reusable_summaries,
)
from summary_schema import normalize_ai_summary


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


def _make_canonical_summary(
    *,
    title: str,
    authors: list[str],
    year: str,
    doi: str,
) -> dict:
    paper_info = {
        "title": title,
        "authors": authors,
        "year": year,
        "journal": "Journal",
        "doi": doi,
        "canonical_paper_key": normalize_doi(doi) or build_paper_key(
            {"title": title, "authors": authors, "year": year}
        ),
    }
    return {
        "paper_info": paper_info,
        "status": "success",
        "ai_summary": normalize_ai_summary(
            {
                "paper_metadata": paper_info,
                "core_analysis": {
                    "summary": "Canonical reusable summary.",
                    "methodology": "Archival analysis.",
                    "findings": "The source supports reuse.",
                    "conclusions": "Reuse remains audited.",
                },
            }
        ),
    }


def _make_generator(tmp_path: Path, output_root: Path, *, project_name: str = "current") -> main.LiteratureReviewGenerator:
    current_workspace = output_root / f"{project_name}__job999" / "artifacts"
    current_workspace.mkdir(parents=True, exist_ok=True)
    generator = main.LiteratureReviewGenerator(project_name=project_name, pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict({"Paths": {"output_path": str(output_root)}})
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
    assert reused_summary["paper_info"].get("doi") == "10.1000/demo"
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
    reuse_report = generator._stage1_reuse_report
    assert reuse_report is not None
    assert {item["match_type"] for item in reuse_report["reused_papers"]} == {
        "doi_exact",
        "canonical_paper_key_exact",
    }
    winner_paths = {item["winner_source"]["path"] for item in reuse_report["reused_papers"]}
    assert any(path.endswith("first_summaries.json") for path in winner_paths)
    assert any(path.endswith("second_summaries.json") for path in winner_paths)
    assert reuse_report["preview"]["needs_analysis_count"] == 1


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
    reuse_report = generator._stage1_reuse_report
    assert reuse_report is not None
    assert reuse_report["not_reused"][0]["reason"] == "ambiguous_match"
    assert len(reuse_report["not_reused"][0]["ambiguous_candidates"]) == 2


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
    generator.config = ConfigDict({
        "Paths": {"output_path": str(output_root)},
        "Performance": {"max_workers": "1"},
    })
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


def test_index_reusable_summaries_skips_incomplete_stage1_inputs(tmp_path: Path) -> None:
    stage1_input_path = tmp_path / "short_stage1_input.md"
    stage1_input_path.write_text("Short extracted paper text. " * 150, encoding="utf-8")
    manifest_path = tmp_path / "stage1_input_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "selected_text_source": "normalized_markdown",
                "stage1_quality_level": "PASS",
                "stage1_quality_reasons": [],
                "selected_text_length": len(stage1_input_path.read_text(encoding="utf-8")),
                "page_count": 11,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    summary_path = tmp_path / "summaries.json"
    summary = _make_summary(
        title="Reusable But Incomplete",
        authors=["Alice Example"],
        year="2024",
        doi="10.1000/incomplete",
    )
    summary["preprocess"] = {
        "selected_text_source": "normalized_markdown",
        "stage1_quality_level": "PASS",
        "stage1_quality_reasons": [],
        "stage1_input_path": str(stage1_input_path),
        "stage1_input_manifest_path": str(manifest_path),
    }
    summary_path.write_text(json.dumps([summary], ensure_ascii=False), encoding="utf-8")

    reusable, rejected = index_reusable_summaries(
        [
            SummarySource(
                path=str(summary_path),
                source_type="explicit",
                priority=0,
                label="explicit",
            )
        ]
    )

    assert reusable == {}
    assert rejected[0]["reason"] == "stage1_input_incomplete_or_blocked"


def test_summary_internal_paths_resolve_from_summary_source_not_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    cwd_dir = tmp_path / "unrelated-cwd"
    cwd_dir.mkdir()

    relative_input = Path("preprocess/stage1_input.md")
    relative_manifest = Path("preprocess/stage1_input_manifest.json")
    relative_quality = Path("preprocess/stage1_quality_report.json")

    source_input = source_dir / relative_input
    source_input.parent.mkdir()
    source_text = "Grounded source text with discussion and references. " * 110
    source_input.write_text(source_text, encoding="utf-8")
    (source_dir / relative_manifest).write_text(
        json.dumps({"page_count": 4, "selected_text_length": len(source_text)}),
        encoding="utf-8",
    )
    (source_dir / relative_quality).write_text(
        json.dumps(
            {
                "candidate_reports": [
                    {"source": "normalized_markdown", "text_length": len(source_text)}
                ]
            }
        ),
        encoding="utf-8",
    )

    decoy_input = cwd_dir / relative_input
    decoy_input.parent.mkdir()
    decoy_input.write_text("decoy", encoding="utf-8")
    (cwd_dir / relative_manifest).write_text(
        json.dumps({"page_count": 10, "selected_text_length": 5}),
        encoding="utf-8",
    )
    (cwd_dir / relative_quality).write_text(
        json.dumps(
            {
                "candidate_reports": [
                    {"source": "longer_decoy", "text_length": 20000}
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = _make_summary(
        title="Relative Source Paths",
        authors=["Alice Example"],
        year="2024",
        doi="10.1000/relative-source",
    )
    summary["preprocess"] = {
        "stage1_quality_level": "PASS",
        "stage1_quality_reasons": [],
        "stage1_input_path": str(relative_input),
        "stage1_input_manifest_path": str(relative_manifest),
        "stage1_quality_report_path": str(relative_quality),
    }
    summary_path = source_dir / "summaries.json"
    summary_path.write_text(json.dumps([summary]), encoding="utf-8")
    monkeypatch.chdir(cwd_dir)

    reusable, rejected = index_reusable_summaries(
        [
            SummarySource(
                path=str(summary_path),
                source_type="explicit",
                priority=0,
                label="source-relative",
            )
        ]
    )

    assert set(reusable) == {"10.1000/relative-source"}
    assert rejected == []


def test_load_existing_summaries_materializes_summary_file_override(tmp_path: Path) -> None:
    external_summary = tmp_path / "subset.json"
    external_summary.write_text(
        json.dumps(
            [
                _make_canonical_summary(
                    title="Subset Paper",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/subset",
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
    generator.summary_file_override = str(external_summary)

    assert generator.load_existing_summaries() is True
    assert generator.summaries[0]["paper_info"].get("title") == "Subset Paper"
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
                _make_canonical_summary(
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
                _make_canonical_summary(
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
    titles = {item["paper_info"].get("title") for item in generator.summaries}
    assert titles == {"Subset Paper A", "Subset Paper B"}

    manifest_path = Path(generator._get_summary_source_manifest_path())
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["source_kind"] == "explicit_summary_sources"
    assert len(manifest_payload["source_items"]) == 2


def test_explicit_legacy_summary_reuse_writes_unified_audit_record(tmp_path: Path) -> None:
    external_summary = tmp_path / "legacy-summary.json"
    raw_summary = _make_canonical_summary(
        title="Audited Paper",
        authors=["Alice Example"],
        year="2024",
        doi="10.1000/audited",
    )
    external_summary.write_text(
        json.dumps([raw_summary]),
        encoding="utf-8",
    )
    workspace = JobWorkspace.create(str(tmp_path / "output"), "audit", job_id="job-audit")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = main.LiteratureReviewGenerator(project_name="audit", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.job_workspace = workspace
    generator.artifact_registry = registry
    generator.output_dir = workspace.root_dir
    generator.summary_file = workspace.artifact_path("audit_summaries.json")
    generator.summary_file_override = str(external_summary)
    generator.audit_actor = "test-operator"
    generator.audit_reason = "selected verified historical summary"
    generator.audit_scope = {"source": "test_fixture"}

    first_projection = generator._project_legacy_summary_to_canonical(raw_summary)
    second_projection = generator._project_legacy_summary_to_canonical(raw_summary)
    assert first_projection == second_projection

    assert generator.load_existing_summaries() is True
    assert json.loads(external_summary.read_text(encoding="utf-8")) == [raw_summary]

    audit_records = [record for record in registry.list_records() if record.artifact_type == "audit_record"]
    assert len(audit_records) == 1
    audit = AuditRecordV1.from_dict(
        json.loads(Path(audit_records[0].path).read_text(encoding="utf-8"))
    )
    assert audit.audit_type == "legacy_reuse"
    assert audit.actor == "test-operator"
    assert audit.reason == "selected verified historical summary"
    assert audit.scope["source_count"] == 1
    assert audit.scope["canonical_projection"] == generator.LEGACY_SUMMARY_PROJECTION_VERSION
    assert len(audit.input_artifact_refs) == 1
    assert len(audit.output_artifact_refs) == 1
    source_record = next(
        record for record in registry.list_records() if record.artifact_role == "legacy_summary_source"
    )
    assert source_record.artifact_type == "legacy_summary_source"
    assert source_record.artifact_version == "v1"
    assert audit.input_hashes == {"summary_source_1": source_record.content_hash}
    summary_record = next(
        record for record in registry.list_records() if record.artifact_type == "summary_file"
    )
    assert audit.output_artifact_refs[0].content_hash == summary_record.content_hash
    assert json.loads(Path(summary_record.path).read_text(encoding="utf-8")) == [first_projection]
    assert {dependency.artifact_id for dependency in audit_records[0].depends_on} == {
        source_record.artifact_id,
        "summary_file:audit_summaries.json",
    }
    reconciler = RuntimeReconciler(workspace, registry)
    reconciler.validate_record(summary_record)
    reconciler.validate_record(audit_records[0])


def test_explicit_legacy_summary_audit_failure_remains_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_summary = tmp_path / "legacy-summary.json"
    external_summary.write_text(
        json.dumps(
            [
                _make_canonical_summary(
                    title="Audit Failure Paper",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/audit-failure",
                )
            ]
        ),
        encoding="utf-8",
    )
    workspace = JobWorkspace.create(str(tmp_path / "output"), "audit-failure", job_id="job-audit-failure")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = main.LiteratureReviewGenerator(project_name="audit-failure", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.job_workspace = workspace
    generator.artifact_registry = registry
    generator.output_dir = workspace.root_dir
    generator.summary_file = workspace.artifact_path("audit-failure_summaries.json")
    generator.summary_file_override = str(external_summary)

    original_register = registry.register_file

    def fail_audit_registration(**kwargs):
        if kwargs.get("artifact_type") == "audit_record":
            raise RuntimeError("injected audit registration failure")
        return original_register(**kwargs)

    monkeypatch.setattr(registry, "register_file", fail_audit_registration)

    assert generator.load_existing_summaries() is False
    assert generator.summaries == []
    records = registry.list_records()
    assert records
    assert {record.status for record in records} == {"quarantined"}
    assert not any(record.artifact_type == "audit_record" for record in records)

    generator.summary_file_override = None
    generator.summary_source_overrides = []
    assert generator.load_existing_summaries() is False
    assert generator.summaries == []
    assert {record.status for record in registry.list_records()} == {"quarantined"}

    monkeypatch.setattr(registry, "register_file", original_register)
    generator.summary_file_override = str(external_summary)
    assert generator.load_existing_summaries() is True
    assert generator.summaries
    assert {record.status for record in registry.list_records()} == {"ready"}
    assert any(record.artifact_type == "audit_record" for record in registry.list_records())


def test_stage_one_aborts_before_source_or_provider_work_when_summary_load_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = main.LiteratureReviewGenerator(project_name="unsafe-resume", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.config = ConfigDict({"Paths": {}})
    calls = {"scan": 0, "provider": 0}

    monkeypatch.setattr(generator, "load_configuration", lambda: True)
    monkeypatch.setattr(generator, "setup_output_directory", lambda: True)
    monkeypatch.setattr(generator, "load_checkpoint", lambda: False)
    monkeypatch.setattr(generator, "load_existing_summaries", lambda: False)

    def scan_pdf_folder() -> bool:
        calls["scan"] += 1
        return True

    def process_all_papers() -> bool:
        calls["provider"] += 1
        return True

    monkeypatch.setattr(generator, "scan_pdf_folder", scan_pdf_folder)
    monkeypatch.setattr(generator, "process_all_papers", process_all_papers)

    assert generator.run_stage_one() is False
    assert calls == {"scan": 0, "provider": 0}


@pytest.mark.parametrize(
    "ai_summary",
    [
        {},
        {
            "core_analysis": {
                "summary": "Only one critical field is present.",
            }
        },
    ],
    ids=("empty", "missing-critical-fields"),
)
def test_legacy_projection_rejects_content_incomplete_summary_before_writes(
    tmp_path: Path,
    ai_summary: dict,
) -> None:
    external_summary = tmp_path / "incomplete-legacy-summary.json"
    external_summary.write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "paper_info": {
                        "title": "Incomplete Legacy Summary",
                        "authors": ["Alice Example"],
                        "year": "2024",
                        "doi": "10.1000/incomplete",
                    },
                    "ai_summary": ai_summary,
                }
            ]
        ),
        encoding="utf-8",
    )
    workspace = JobWorkspace.create(str(tmp_path / "output"), "incomplete", job_id="job-incomplete")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = main.LiteratureReviewGenerator(project_name="incomplete", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.job_workspace = workspace
    generator.artifact_registry = registry
    generator.output_dir = workspace.root_dir
    generator.summary_file = workspace.artifact_path("incomplete_summaries.json")
    generator.summary_file_override = str(external_summary)

    assert generator.load_existing_summaries() is False
    assert generator.summaries == []
    assert registry.list_records() == []
    assert not Path(workspace.paths.registry_path).exists()
    assert not Path(generator.summary_file).exists()
    assert not Path(generator._get_summary_source_manifest_path()).exists()


def test_mixed_legacy_summary_sources_fail_atomically_before_writes(tmp_path: Path) -> None:
    valid_source = tmp_path / "valid-legacy-summary.json"
    invalid_source = tmp_path / "invalid-legacy-summary.json"
    valid_source.write_text(
        json.dumps(
            [
                _make_canonical_summary(
                    title="Valid Legacy Summary",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/valid-legacy",
                )
            ]
        ),
        encoding="utf-8",
    )
    invalid_source.write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "paper_info": {
                        "title": "Invalid Legacy Summary",
                        "authors": ["Bob Example"],
                        "year": "2023",
                        "doi": "10.1000/invalid-legacy",
                    },
                    "ai_summary": {},
                }
            ]
        ),
        encoding="utf-8",
    )
    workspace = JobWorkspace.create(str(tmp_path / "output"), "mixed", job_id="job-mixed")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = main.LiteratureReviewGenerator(project_name="mixed", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.job_workspace = workspace
    generator.artifact_registry = registry
    generator.output_dir = workspace.root_dir
    generator.summary_file = workspace.artifact_path("mixed_summaries.json")
    generator.summary_file_override = str(valid_source)
    generator.summary_source_overrides = [str(invalid_source)]

    assert generator.load_existing_summaries() is False
    assert generator.summaries == []
    assert registry.list_records() == []
    assert not Path(workspace.paths.registry_path).exists()
    assert not Path(generator.summary_file).exists()
    assert not Path(generator._get_summary_source_manifest_path()).exists()


def test_low_quality_duplicate_loser_fails_before_writes(tmp_path: Path) -> None:
    valid_source = tmp_path / "valid-duplicate.json"
    weak_duplicate_source = tmp_path / "weak-duplicate.json"
    valid_source.write_text(
        json.dumps(
            [
                _make_canonical_summary(
                    title="Duplicate Winner",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/duplicate-quality-gate",
                )
            ]
        ),
        encoding="utf-8",
    )
    weak_duplicate_source.write_text(
        json.dumps(
            [
                {
                    "status": "success",
                    "paper_info": {
                        "title": "Duplicate Loser",
                        "authors": ["Bob Example"],
                        "year": "2023",
                        "doi": "10.1000/duplicate-quality-gate",
                    },
                    "ai_summary": {"core_analysis": {"summary": "Too weak to reuse."}},
                }
            ]
        ),
        encoding="utf-8",
    )
    workspace = JobWorkspace.create(str(tmp_path / "output"), "duplicate", job_id="job-duplicate")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = main.LiteratureReviewGenerator(project_name="duplicate", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.job_workspace = workspace
    generator.artifact_registry = registry
    generator.output_dir = workspace.root_dir
    generator.summary_file = workspace.artifact_path("duplicate_summaries.json")
    generator.summary_file_override = str(valid_source)
    generator.summary_source_overrides = [str(weak_duplicate_source)]

    assert generator.load_existing_summaries() is False
    assert generator.summaries == []
    assert registry.list_records() == []
    assert not Path(workspace.paths.registry_path).exists()
    assert not Path(generator.summary_file).exists()
    assert not Path(generator._get_summary_source_manifest_path()).exists()


def test_nonreusable_legacy_source_is_excluded_from_ready_dependency_chain(
    tmp_path: Path,
) -> None:
    valid_source = tmp_path / "valid-source.json"
    failed_source = tmp_path / "failed-source.json"
    valid_source.write_text(
        json.dumps(
            [
                _make_canonical_summary(
                    title="Reusable Source",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/reusable-source",
                )
            ]
        ),
        encoding="utf-8",
    )
    failed_source.write_text(
        json.dumps(
            [
                _make_summary(
                    title="Failed Source",
                    authors=["Bob Example"],
                    year="2023",
                    doi="10.1000/failed-source",
                    status="failed",
                )
            ]
        ),
        encoding="utf-8",
    )
    workspace = JobWorkspace.create(str(tmp_path / "output"), "filtered", job_id="job-filtered")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = main.LiteratureReviewGenerator(project_name="filtered", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.job_workspace = workspace
    generator.artifact_registry = registry
    generator.output_dir = workspace.root_dir
    generator.summary_file = workspace.artifact_path("filtered_summaries.json")
    generator.summary_file_override = str(valid_source)
    generator.summary_source_overrides = [str(failed_source)]

    assert generator.load_existing_summaries() is True

    source_records = [
        record for record in registry.list_records() if record.artifact_type == "legacy_summary_source"
    ]
    assert [Path(record.path) for record in source_records] == [valid_source.resolve()]
    manifest_record = next(
        record for record in registry.list_records() if record.artifact_type == "summary_source_manifest"
    )
    manifest = json.loads(Path(manifest_record.path).read_text(encoding="utf-8"))
    assert [Path(item["path"]) for item in manifest["source_items"]] == [valid_source.resolve()]
    assert {Path(item["path"]) for item in manifest["rejected_candidates"]} == {
        failed_source.resolve()
    }
    audit_record = next(
        record for record in registry.list_records() if record.artifact_type == "audit_record"
    )
    audit = AuditRecordV1.from_dict(json.loads(Path(audit_record.path).read_text(encoding="utf-8")))
    assert audit.scope["source_count"] == 1
    assert audit.scope["selected_source_count"] == 2
    assert audit.scope["selected_sources"] == (
        {
            "index": 1,
            "path": str(valid_source.resolve()),
            "source_type": "explicit",
            "label": "explicit:1",
            "priority": 0,
            "content_hash": source_records[0].content_hash,
            "contributed": True,
            "rejection_reasons": (),
        },
        {
            "index": 2,
            "path": str(failed_source.resolve()),
            "source_type": "explicit",
            "label": "explicit:2",
            "priority": 1,
            "content_hash": audit.input_hashes["summary_source_2"],
            "contributed": False,
            "rejection_reasons": ("summary_status_not_success",),
        },
    )
    assert audit.input_hashes == {
        "summary_source_1": source_records[0].content_hash,
        "summary_source_2": file_sha256(failed_source),
    }
    assert {ref.artifact_id for ref in audit.input_artifact_refs} == {
        source_records[0].artifact_id
    }
    summary_record = next(
        record for record in registry.list_records() if record.artifact_type == "summary_file"
    )
    assert {dependency.artifact_id for dependency in summary_record.depends_on} == {
        source_records[0].artifact_id
    }
    assert {dependency.artifact_id for dependency in audit_record.depends_on} == {
        source_records[0].artifact_id,
        summary_record.artifact_id,
    }

    reconciler = RuntimeReconciler(workspace, registry)
    for record in (summary_record, manifest_record, audit_record):
        reconciler.validate_record(record)


def test_legacy_summary_source_validator_accepts_doi_or_title_author_year_identity(
    tmp_path: Path,
) -> None:
    workspace = JobWorkspace.create(str(tmp_path / "output"), "legacy", job_id="job-legacy")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    reconciler = RuntimeReconciler(workspace, registry)
    payloads = (
        [
            {
                "status": "success",
                "paper_info": {"doi": "10.1000/doi-only"},
                "ai_summary": {"paper_metadata": {"doi": "10.1000/doi-only"}},
            }
        ],
        [
            {
                "status": "success",
                "paper_info": {
                    "title": "Title Author Year Identity",
                    "authors": ["Alice Example"],
                    "year": "2024",
                },
                "ai_summary": {},
            }
        ],
    )
    for index, payload in enumerate(payloads, start=1):
        path = tmp_path / f"legacy-source-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        record = registry.register_file(
            artifact_role="legacy_summary_source",
            artifact_type="legacy_summary_source",
            artifact_version="v1",
            path=path,
            producer="tests",
            artifact_id=f"legacy-source-{index}",
        )
        reconciler.validate_record(record)


def test_explicit_summary_sources_with_no_reusable_successes_fail_before_writes(
    tmp_path: Path,
) -> None:
    external_summary = tmp_path / "failed-only.json"
    external_summary.write_text(
        json.dumps(
            [
                _make_summary(
                    title="Failed Paper",
                    authors=["Alice Example"],
                    year="2024",
                    doi="10.1000/failed",
                    status="failed",
                )
            ]
        ),
        encoding="utf-8",
    )
    workspace = JobWorkspace.create(str(tmp_path / "output"), "empty", job_id="job-empty")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    generator = main.LiteratureReviewGenerator(project_name="empty", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _DummyLogger())
    generator.job_workspace = workspace
    generator.artifact_registry = registry
    generator.output_dir = workspace.root_dir
    generator.summary_file = workspace.artifact_path("empty_summaries.json")
    generator.summary_file_override = str(external_summary)

    assert generator.load_existing_summaries() is False
    assert generator.summaries == []
    assert registry.list_records() == []
    assert not Path(workspace.paths.registry_path).exists()
    assert not Path(generator.summary_file).exists()
    assert not Path(generator._get_summary_source_manifest_path()).exists()
    with pytest.raises(SummarySourceError, match="no reusable successful summary records"):
        generator._load_summaries_from_sources([str(external_summary)])
