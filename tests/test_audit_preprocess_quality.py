import json
import os
from pathlib import Path
import subprocess
import sys

from tools.audit_preprocess_quality import _encode_for_stdout, main as audit_main


def _make_project_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    output_root = tmp_path / "output"
    pointer_dir = output_root / "demo"
    artifacts_dir = output_root / "demo__job" / "artifacts"
    paper_artifact_dir = artifacts_dir / "paper_artifacts"
    paper_artifact_dir.mkdir(parents=True)
    pointer_dir.mkdir(parents=True)
    (pointer_dir / "_latest_job.json").write_text(
        json.dumps({"workspace_path": str(output_root / "demo__job")}),
        encoding="utf-8",
    )
    return output_root, artifacts_dir, paper_artifact_dir


def test_audit_reports_new_fallback_as_current_warning_without_rewriting(tmp_path: Path) -> None:
    output_root, artifacts_dir, paper_artifact_dir = _make_project_workspace(tmp_path)
    paper_artifact = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "paper_identity": {"canonical_paper_key": "paper-new-fallback"},
        "paper_info": {"title": "New Gated Fallback Paper"},
        "analysis": {
            "status": "success",
            "preprocess": {
                "selected_text_source": "plain_text",
                "stage1_quality_level": "FALLBACK",
                "stage1_quality_reasons": ["cjk_collapse"],
            },
        },
        "stage1_inputs": {},
    }
    (paper_artifact_dir / "paper-new-fallback.json").write_text(
        json.dumps(paper_artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary_path = artifacts_dir / "demo_summaries.json"
    original_summary = '[{"status": "success", "paper_info": {"title": "New Gated Fallback Paper"}}]'
    summary_path.write_text(original_summary, encoding="utf-8")

    exit_code = audit_main(
        [
            "--project",
            "demo",
            "--output-root",
            str(output_root),
            "--mode",
            "all",
            "--write-report",
        ]
    )

    assert exit_code == 0
    bad_inputs = json.loads((artifacts_dir / "bad_stage1_inputs.json").read_text(encoding="utf-8"))
    stale_summaries = json.loads((artifacts_dir / "stale_summaries.json").read_text(encoding="utf-8"))
    audit_report = json.loads((artifacts_dir / "preprocess_quality_audit.json").read_text(encoding="utf-8"))

    record = audit_report["records"][0]
    assert bad_inputs == []
    assert stale_summaries == []
    assert record["paper_key"] == "paper-new-fallback"
    assert record["audit_origin"] == "native_stage1_quality"
    assert record["is_fallback_stage1_input"] is True
    assert record["is_bad_stage1_input"] is False
    assert record["summary_status"] == "current_with_fallback"
    assert audit_report["record_count"] == 1
    assert audit_report["bad_stage1_input_count"] == 0
    assert summary_path.read_text(encoding="utf-8") == original_summary


def test_audit_treats_legacy_rescored_fallback_as_stale(tmp_path: Path) -> None:
    output_root, artifacts_dir, paper_artifact_dir = _make_project_workspace(tmp_path)
    markdown_path = tmp_path / "bad_normalized.md"
    plain_path = tmp_path / "plain_text.txt"
    markdown_path.write_text("short", encoding="utf-8")
    plain_path.write_text(("This legacy plain text is healthy enough for stage one analysis. " * 20), encoding="utf-8")
    paper_artifact = {
        "artifact_type": "paper_artifact",
        "artifact_version": "v1",
        "paper_identity": {"canonical_paper_key": "paper-legacy-fallback"},
        "paper_info": {"title": "Legacy Fallback Paper"},
        "analysis": {
            "status": "success",
            "preprocess": {
                "markdown_path": str(markdown_path),
                "plain_text_path": str(plain_path),
            },
        },
        "stage1_inputs": {},
    }
    (paper_artifact_dir / "paper-legacy-fallback.json").write_text(
        json.dumps(paper_artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    exit_code = audit_main(
        [
            "--project",
            "demo",
            "--output-root",
            str(output_root),
            "--mode",
            "all",
            "--write-report",
        ]
    )

    assert exit_code == 0
    bad_inputs = json.loads((artifacts_dir / "bad_stage1_inputs.json").read_text(encoding="utf-8"))
    stale_summaries = json.loads((artifacts_dir / "stale_summaries.json").read_text(encoding="utf-8"))
    assert bad_inputs[0]["paper_key"] == "paper-legacy-fallback"
    assert bad_inputs[0]["audit_origin"] == "legacy_rescore"
    assert bad_inputs[0]["summary_status"] == "stale_due_to_legacy_bad_stage1_input"
    assert stale_summaries[0]["paper_key"] == "paper-legacy-fallback"


def test_audit_write_report_survives_non_gbk_report_path(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    report_dir = tmp_path / ("reports_" + chr(0xE0FF))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk:strict"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.audit_preprocess_quality",
            "--project",
            "demo",
            "--output-root",
            str(output_root),
            "--write-report",
            "--report-dir",
            str(report_dir),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (report_dir / "preprocess_quality_audit.json").exists()
    assert "\\ue0ff" in result.stdout


def test_encode_for_stdout_escapes_unencodable_characters(monkeypatch) -> None:
    class _Stdout:
        encoding = "gbk"

    monkeypatch.setattr(sys, "stdout", _Stdout())

    assert _encode_for_stdout("x" + chr(0xE0FF)).endswith("\\ue0ff")
