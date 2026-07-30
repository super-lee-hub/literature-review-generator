from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import pph_kalyanaram_refresh as refresh
from services.citation_ref_catalog import build_document_ref_catalog
from summary_schema import normalize_ai_summary


def _write_json(path: Path, payload: Any, *, compact: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    return path


def _summary(doi: str, title: str) -> dict[str, Any]:
    year = "1995" if doi == refresh.TARGET_DOI else "2024"
    authors = ["First Author"]
    ai_summary = normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "panel",
                "classification_status": "resolved",
                "route_confidence": "high",
            },
            "core_analysis": {
                "summary": f"Summary for {title}.",
                "key_points": [f"Key point for {title}."],
                "methodology": "Panel analysis.",
                "findings": "A supported finding.",
                "conclusions": "A bounded conclusion.",
                "relevance": "Relevant to reference prices.",
                "limitations": "Bounded setting.",
            },
            "paper_metadata": {
                "title": title,
                "authors": authors,
                "year": year,
                "journal": "Journal of Tests",
                "doi": doi,
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": ["Does the effect hold?"],
                    "data_source_and_size": "Scanner panel.",
                    "analysis_technique": "Regression.",
                    "core_variables": {
                        "independent": ["Price history"],
                        "dependent": ["Reference price"],
                    },
                    "sample_characteristics_or_context": "Consumer goods.",
                }
            },
        }
    )
    return {
        "paper_info": {
            "title": title,
            "authors": authors,
            "year": year,
            "journal": "Journal of Tests",
            "doi": doi,
            "source_paper_id": doi,
            "canonical_paper_key": doi,
            "paper_key_aliases": [doi, title.casefold()],
        },
        "status": "success",
        "ai_summary": ai_summary,
    }


def _catalog(records: list[dict[str, Any]], project_id: str) -> dict[str, Any]:
    project_name, job_id = refresh.PROJECTS[project_id]
    return build_document_ref_catalog(
        records,
        project_name=project_name,
        job_id=job_id,
    )


@pytest.fixture()
def refresh_env(tmp_path: Path) -> dict[str, Any]:
    paths = refresh.RefreshPaths.from_repo_root(tmp_path)
    a = _summary("10.1000/a", "Paper A")
    b = _summary("10.1000/b", "Paper B")
    c = _summary("10.1000/c", "Paper C")
    d = _summary("10.1000/d", "Paper D")
    e = _summary("10.1000/e", "Paper E")
    source = _summary(refresh.TARGET_DOI, "Empirical Generalizations")

    payloads = {
        "master_summaries": [a, b, c, d, e],
        "subset_02": [a],
        "subset_90": [b],
        "subset_91": [c],
        "subset_03": [a, d],
        "subset_05": [e],
        "s02_summary": [a, b, c],
        "s03_summary": [a, d, b],
        "s05_summary": [e, a, d],
    }
    for role, path in paths.summary_inputs().items():
        _write_json(path, payloads[role])
    _write_json(paths.s02_catalog, _catalog(payloads["s02_summary"], "S02"))
    _write_json(paths.s03_catalog, _catalog(payloads["s03_summary"], "S03"))
    _write_json(paths.s05_catalog, _catalog(payloads["s05_summary"], "S05"))
    source_path = _write_json(tmp_path / "source" / "kalyanaram.json", [source])

    expected_counts = {
        **{role: len(records) for role, records in payloads.items()},
        "s02_catalog": len(payloads["s02_summary"]),
        "s03_catalog": len(payloads["s03_summary"]),
        "s05_catalog": len(payloads["s05_summary"]),
    }
    return {
        "paths": paths,
        "payloads": payloads,
        "source": source,
        "source_path": source_path,
        "expected_counts": expected_counts,
        "tmp_path": tmp_path,
    }


def _write_item(plan: dict[str, Any], role: str) -> dict[str, Any]:
    return next(item for item in plan["writes"] if item["role"] == role)


def test_prepare_refresh_is_dry_run_and_materializes_exact_dependencies(
    refresh_env: dict[str, Any],
) -> None:
    paths = refresh_env["paths"]
    before_hashes = {
        role: refresh._file_sha256(path)
        for role, path in {**paths.summary_inputs(), **paths.catalog_inputs()}.items()
    }

    prepared = refresh.prepare_refresh(
        paths,
        refresh_env["source_path"],
        expected_pre_counts=refresh_env["expected_counts"],
    )

    assert refresh.validate_plan(prepared.plan) == prepared.plan
    assert _write_item(prepared.plan, "master_summaries")["after"]["count"] == 6
    assert _write_item(prepared.plan, "subset_02")["after"]["count"] == 2
    assert _write_item(prepared.plan, "s02_summary")["after"]["count"] == 4
    assert _write_item(prepared.plan, "s03_summary")["after"]["count"] == 4
    assert _write_item(prepared.plan, "s05_summary")["after"]["count"] == 4
    assert prepared.plan["catalog_ref_ids"] == {
        "S02": "R004",
        "S03": "R004",
        "S05": "R004",
    }
    assert [
        record["paper_info"]["doi"] for record in prepared.payloads["s02_summary"]
    ] == [
        "10.1000/a",
        refresh.TARGET_DOI,
        "10.1000/b",
        "10.1000/c",
    ]
    assert [
        record["paper_info"]["doi"] for record in prepared.payloads["s05_summary"]
    ] == [
        "10.1000/e",
        "10.1000/a",
        refresh.TARGET_DOI,
        "10.1000/d",
    ]
    after_hashes = {
        role: refresh._file_sha256(path)
        for role, path in {**paths.summary_inputs(), **paths.catalog_inputs()}.items()
    }
    assert after_hashes == before_hashes


def test_catalogs_preserve_old_refs_and_append_source_after_historical_max(
    refresh_env: dict[str, Any],
) -> None:
    prepared = refresh.prepare_refresh(
        refresh_env["paths"],
        refresh_env["source_path"],
        expected_pre_counts=refresh_env["expected_counts"],
    )

    for project_id in ("S02", "S03", "S05"):
        role = f"{project_id.casefold()}_catalog"
        before = json.loads(
            refresh_env["paths"].catalog_inputs()[role].read_text(encoding="utf-8")
        )
        old_refs = {entry["doi"]: entry["ref_id"] for entry in before["entries"]}
        after_refs = {
            entry["doi"]: entry["ref_id"]
            for entry in prepared.payloads[role]["entries"]
            if entry["status"] == "active"
        }
        assert {doi: after_refs[doi] for doi in old_refs} == old_refs
        assert after_refs[refresh.TARGET_DOI] == "R004"


def test_apply_requires_unchanged_plan_and_creates_verified_backup(
    refresh_env: dict[str, Any],
) -> None:
    prepared = refresh.prepare_refresh(
        refresh_env["paths"],
        refresh_env["source_path"],
        expected_pre_counts=refresh_env["expected_counts"],
    )

    result = refresh.apply_refresh(
        refresh_env["paths"],
        refresh_env["source_path"],
        prepared.plan,
        expected_pre_counts=refresh_env["expected_counts"],
        backup_root=refresh_env["tmp_path"] / "output" / "backups",
    )

    assert result["status"] == "committed"
    assert result["requires_post_refresh_reconcile"] == ["S02", "S03", "S05"]
    backup_dir = Path(result["backup_dir"])
    backup_manifest = json.loads(
        (backup_dir / "backup_manifest.json").read_text(encoding="utf-8")
    )
    assert backup_manifest["status"] == "committed"
    assert len(backup_manifest["files"]) == 8
    for row in backup_manifest["files"]:
        assert refresh._file_sha256(Path(row["backup_path"])) == row["sha256"]
    for item in prepared.plan["writes"]:
        assert refresh._file_sha256(Path(item["path"])) == item["after"]["sha256"]


def test_apply_fails_closed_when_any_planned_input_hash_drifts(
    refresh_env: dict[str, Any],
) -> None:
    prepared = refresh.prepare_refresh(
        refresh_env["paths"],
        refresh_env["source_path"],
        expected_pre_counts=refresh_env["expected_counts"],
    )
    master_before = refresh._file_sha256(refresh_env["paths"].master_summaries)
    subset_90 = refresh_env["paths"].subset_90
    _write_json(subset_90, refresh_env["payloads"]["subset_90"], compact=True)

    with pytest.raises(
        refresh.RefreshError, match="does not match the accepted dry-run plan"
    ):
        refresh.apply_refresh(
            refresh_env["paths"],
            refresh_env["source_path"],
            prepared.plan,
            expected_pre_counts=refresh_env["expected_counts"],
            backup_root=refresh_env["tmp_path"] / "output" / "backups",
        )

    assert refresh._file_sha256(refresh_env["paths"].master_summaries) == master_before
    assert not (refresh_env["tmp_path"] / "output" / "backups").exists()


def test_prepare_fails_closed_on_conflicting_existing_target_doi(
    refresh_env: dict[str, Any],
) -> None:
    master = list(refresh_env["payloads"]["master_summaries"])
    conflicting = dict(refresh_env["source"])
    conflicting["paper_info"] = dict(conflicting["paper_info"])
    conflicting["paper_info"]["title"] = "Conflicting title"
    master.append(conflicting)
    _write_json(refresh_env["paths"].master_summaries, master)
    counts = dict(refresh_env["expected_counts"])
    counts["master_summaries"] += 1

    with pytest.raises(refresh.RefreshError, match="already contains DOI"):
        refresh.prepare_refresh(
            refresh_env["paths"],
            refresh_env["source_path"],
            expected_pre_counts=counts,
        )


def test_source_must_be_one_canonical_stage1_record(
    refresh_env: dict[str, Any],
) -> None:
    _write_json(
        refresh_env["source_path"], [refresh_env["source"], refresh_env["source"]]
    )

    with pytest.raises(refresh.RefreshError, match="exactly one record"):
        refresh.prepare_refresh(
            refresh_env["paths"],
            refresh_env["source_path"],
            expected_pre_counts=refresh_env["expected_counts"],
        )
