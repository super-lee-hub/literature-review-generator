from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping
import zipfile

import pytest

from services.artifact_registry import file_sha256
from services.review_batch import ReviewBatchSpecV1, SummarySelectionSpecV1
from runtime.job_spec import load_runtime_job_spec
from runtime.runner import AgentRuntimeRunner
from tests import synthetic_runtime_fakes


def _run_cli_process(
    repo: Path,
    *args: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "runtime.cli", *args],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=180,
    )


def _run_cli(
    repo: Path,
    *args: str,
    env: dict[str, str],
    expected_returncodes: tuple[int, ...] = (0,),
) -> dict:
    result = _run_cli_process(repo, *args, env=env)
    assert result.returncode in expected_returncodes, result.stdout + "\n" + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _write_spec(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _provider_calls(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _offline_env(counter: Path, **overrides: str) -> dict[str, str]:
    inherited_names = (
        "APPDATA",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    env = {name: os.environ[name] for name in inherited_names if name in os.environ}
    env.update(
        {
            "AUTO_GENERATE_STRICT_OFFLINE": "1",
            "LLM_BACKUP_READER_API": "offline-fixture",
            "LLM_FREE_MODE_API": "offline-fixture",
            "LLM_OUTLINE_API": "offline-fixture",
            "LLM_PRIMARY_READER_API": "offline-fixture",
            "LLM_VALIDATOR_API": "offline-fixture",
            "LLM_WRITER_API": "offline-fixture",
            "PYTHONIOENCODING": "utf-8",
            "SYNTHETIC_PROVIDER_COUNTER": str(counter),
        }
    )
    env.update(overrides)
    return env


def _read_registry(workspace: Path) -> dict:
    return json.loads((workspace / "artifact_registry.json").read_text(encoding="utf-8"))


def _artifact_by_type(registry_payload: dict, artifact_type: str) -> dict:
    return next(
        item for item in registry_payload["artifacts"] if item["artifact_type"] == artifact_type
    )


def _attempt_history(workspace: Path) -> list[dict]:
    attempts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (workspace / "artifacts" / "job_attempts").glob("snapshot-*.json")
    ]
    return sorted(attempts, key=lambda item: item["snapshot_sequence"])


def _assert_latest_pointer(output: Path, project_name: str, result: dict) -> None:
    pointer = json.loads(
        (output / project_name / "_latest_job.json").read_text(encoding="utf-8")
    )
    assert pointer["project_name"] == project_name
    assert pointer["job_id"] == result["job_id"]
    assert Path(pointer["workspace_path"]).resolve() == Path(result["workspace_path"]).resolve()
    assert pointer["status"] == result["job_status"]


def _assert_ready_artifact_graph(
    registry_payload: dict,
    registries_by_job: dict[str, dict],
) -> None:
    records_by_job = {
        job_id: {item["artifact_id"]: item for item in payload["artifacts"]}
        for job_id, payload in registries_by_job.items()
    }
    for record in registry_payload["artifacts"]:
        if record["status"] != "ready":
            continue
        artifact_path = Path(record["path"])
        assert artifact_path.is_file(), record["artifact_id"]
        for dependency in record["depends_on"]:
            dependency_job = dependency["job_id"]
            assert dependency_job in records_by_job, dependency
            target = records_by_job[dependency_job].get(dependency["artifact_id"])
            assert target is not None, dependency
            assert target["status"] == "ready"
            assert target["artifact_type"] == dependency["artifact_type"]
            assert Path(target["path"]).resolve() == Path(dependency["path"]).resolve()
            assert target["content_hash"] == dependency["content_hash"]
            assert file_sha256(dependency["path"]) == dependency["content_hash"]


def _minimal_text_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _write_config(path: Path, output: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "[Paths]",
                f"output_path = {output}",
                "[Primary_Reader_API]",
                "api_key = offline-fixture",
                "model = fixture",
                "api_base = https://example.invalid/v1",
                "[Backup_Reader_API]",
                "api_key = offline-fixture",
                "model = fixture",
                "api_base = https://example.invalid/v1",
                "[Writer_API]",
                "api_key = offline-fixture",
                "model = fixture",
                "api_base = https://example.invalid/v1",
                "[Outline_API]",
                "api_key = offline-fixture",
                "model = fixture",
                "api_base = https://example.invalid/v1",
                "[Validator_API]",
                "api_key = offline-fixture",
                "model = fixture",
                "api_base = https://example.invalid/v1",
                "[Outline]",
                "enable_outline_intelligence_v2 = true",
                "test_dev_fixture_mode = true",
                "require_explicit_adopt = true",
                "candidate_count = 3",
                "[Validation]",
                "stage1_enabled = false",
                "stage2_enabled = true",
                "[Performance]",
                "enable_stage2_validation = true",
                "[Styling]",
                "font_name = Times New Roman",
                "font_size_body = 12",
                "font_size_heading1 = 16",
                "font_size_heading2 = 14",
            ]
        ),
        encoding="utf-8",
    )


def test_public_runtime_cli_synthetic_parent_and_derived_batches(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "中文 synthetic workspace"
    papers = root / "论文 fixtures"
    papers.mkdir(parents=True)
    for index in range(1, 62):
        (papers / f"paper-{index:03d}.pdf").write_bytes(b"%PDF-1.4\n% synthetic\n")
    output = root / "output"
    config = root / "config.ini"
    counter = root / "provider-calls.jsonl"
    _write_config(config, output)
    env = _offline_env(counter)
    parent_spec = root / "parent.json"
    _write_spec(
        parent_spec,
        {
            "project_name": "synthetic-parent",
            "source": {"mode": "direct", "pdf_folder": str(papers)},
            "config": str(config),
            "action": "run_all",
            "queue_file": str(root / "queue.json"),
            "metadata": {
                "requested_stages": ["analyze", "outline", "review", "validate"],
                "validation_required": True,
                "require_clean_validation": True,
            },
        },
    )
    parent = _run_cli(
        repo,
        "run",
        str(parent_spec),
        "--stage-handler",
        "tests.synthetic_runtime_fakes:stage_handler",
        "--validator-module",
        "tests.synthetic_runtime_fakes",
        env=env,
    )
    assert parent["job_status"] == "completed"
    assert parent["job_disposition"] == "clean"
    assert parent["canonical_ready"] is True
    assert "中文 synthetic workspace" in parent["workspace_path"]
    assert "\ufffd" not in parent["workspace_path"]
    workspace = Path(parent["workspace_path"])
    registry_payload = _read_registry(workspace)
    registries_by_job = {parent["job_id"]: registry_payload}
    records = {item["artifact_id"]: item for item in registry_payload["artifacts"]}
    summary_record = next(item for item in records.values() if item["artifact_role"] == "summary")
    summary_path = Path(summary_record["path"])
    summaries = json.loads(summary_path.read_text(encoding="utf-8"))
    assert len(summaries) == 61
    parent_keys = tuple(
        item["paper_info"]["canonical_paper_key"] for item in summaries
    )
    assert len([line for line in counter.read_text(encoding="utf-8").splitlines() if '"stage1"' in line]) == 61
    for artifact_id in (
        "outline_stage_health",
        "adopted_final_outline",
        "review_draft_v2:full_review",
        "citation_manifest:v3",
    ):
        assert artifact_id in records
    review_draft_record = records["review_draft_v2:full_review"]
    review_draft = json.loads(Path(review_draft_record["path"]).read_text(encoding="utf-8"))
    assert review_draft["artifact_version"] == "v2"
    block = review_draft["content"]["sections"][0]["blocks"][0]
    sentences = block["span_map"]["sentences"]
    assert [item["text"] for item in sentences] == [
        "第一句。",
        "第二句 reports a bilingual effect. [[cite_ref:R001]]",
    ]
    assert [item["span_start"] for item in sentences] == [0, 4]
    assert all(
        block["text"][item["span_start"] : item["span_end"]] == item["raw_text"]
        for item in sentences
    )

    citation_manifest = json.loads(
        Path(records["citation_manifest:v3"]["path"]).read_text(encoding="utf-8")
    )
    assert citation_manifest["artifact_version"] == "v3"
    assert citation_manifest["occurrences"][0]["paper_id"] == parent_keys[0]
    claim_unit = citation_manifest["citation_sets"][0]["claim_units"][0]
    assert claim_unit["sentence_index"] == 2
    assert claim_unit["span_start"] == 4
    assert claim_unit["claim_text"] == "第二句 reports a bilingual effect."

    review_docx = _artifact_by_type(registry_payload, "review_docx")
    assert zipfile.is_zipfile(review_docx["path"])
    validation = next(item for item in records.values() if item["artifact_type"] == "validation_run_result")
    validation_hash = validation["content_hash"]
    assert validation_hash == file_sha256(validation["path"])
    validation_payload = json.loads(Path(validation["path"]).read_text(encoding="utf-8"))
    assert validation_payload["validation_disposition"] == "clean"
    assert validation_payload["total_claims"] == 1
    validation_claim = validation_payload["claim_results"][0]
    assert validation_claim["citation_set_key"] == parent_keys[0]
    retrieval_queries = validation_claim["details"]["retrieval_queries"]
    assert retrieval_queries[0] == "第二句展示双语效应。"
    assert any("bilingual effect" in query for query in retrieval_queries[1:])
    assert any(
        item["match_reason"].startswith("bilingual_retrieval:")
        and "bilingual effect" in item["text_excerpt"]
        for item in validation_claim["evidence_candidates"]
    )
    expected_projection = {
        "source_validation_run_hash": validation_hash,
        "validation_run_id": validation_payload["validation_run_id"],
        "execution_status": validation_payload["execution_status"],
        "validation_disposition": validation_payload["validation_disposition"],
        "claim_verdict_counts": validation_payload["claim_verdict_counts"],
        "total_claims": validation_payload["total_claims"],
        "contradicted_count": validation_payload["contradicted_count"],
        "claim_results": validation_payload["claim_results"],
    }
    projection_payloads = []
    for artifact_type in (
        "manual_review_projection",
        "validation_completion_projection",
        "claim_alignment_audit_projection",
    ):
        projection = next(item for item in records.values() if item["artifact_type"] == artifact_type)
        payload = json.loads(Path(projection["path"]).read_text(encoding="utf-8"))
        projection_payloads.append(payload)
        assert payload == expected_projection
    assert projection_payloads[0] == projection_payloads[1] == projection_payloads[2]
    assert [item["status"] for item in _attempt_history(workspace)] == [
        "pending",
        "running",
        "succeeded",
    ]
    _assert_latest_pointer(output, "synthetic-parent", parent)

    selected = {"ABC": parent_keys, "A": parent_keys[:20], "AB": parent_keys[:45]}
    child_hashes = set()
    for label, keys in selected.items():
        batch_path = root / f"batch-{label}.json"
        batch = ReviewBatchSpecV1(
            project_name=f"synthetic-{label}",
            batch_label=label,
            selection=SummarySelectionSpecV1(
                parent_job_id=parent["job_id"],
                parent_registry_path=str(workspace / "artifact_registry.json"),
                parent_artifact_id=summary_record["artifact_id"],
                parent_content_hash=summary_record["content_hash"],
                parent_summary_path=str(summary_path),
                ordered_paper_keys=keys,
                expected_count=len(keys),
            ),
        )
        _write_spec(batch_path, batch.to_dict())
        child_spec = root / f"child-{label}.json"
        _write_spec(
            child_spec,
            {
                "project_name": f"synthetic-{label}",
                "source": {"mode": "direct", "pdf_folder": str(papers)},
                "config": str(config),
                "action": "analyze",
                "queue_file": str(root / f"queue-{label}.json"),
                "metadata": {
                    "requested_stages": ["analyze"],
                    "review_batch_spec": str(batch_path),
                },
            },
        )
        child = _run_cli(repo, "run", str(child_spec), env=env)
        assert child["job_status"] == "completed"
        child_workspace = Path(child["workspace_path"])
        child_registry = _read_registry(child_workspace)
        registries_by_job[child["job_id"]] = child_registry
        child_summary = next(
            item for item in child_registry["artifacts"] if item["artifact_role"] == "summary"
        )
        child_payload = json.loads(Path(child_summary["path"]).read_text(encoding="utf-8"))
        assert len(child_payload) == len(keys)
        child_hashes.add(next(
            dep["content_hash"]
            for dep in child_summary["depends_on"]
            if dep["dependency_kind"] == "external_job" and dep["artifact_id"] == summary_record["artifact_id"]
        ))
        analyze_terminal = next(
            json.loads(Path(item["path"]).read_text(encoding="utf-8"))
            for item in child_registry["artifacts"]
            if item["artifact_type"] == "runtime_stage_terminal"
            and item["metadata"].get("stage_name") == "analyze"
        )
        assert analyze_terminal["model_call_count"] == 0
        assert [item["status"] for item in _attempt_history(child_workspace)] == [
            "pending",
            "running",
            "succeeded",
        ]
        _assert_latest_pointer(output, f"synthetic-{label}", child)
    assert child_hashes == {summary_record["content_hash"]}
    calls = _provider_calls(counter)
    assert sum(item["kind"] == "stage1" for item in calls) == 61
    assert sum(item["kind"] == "stage2" for item in calls) == 1
    assert sum(item["kind"] == "stage3" for item in calls) == 1

    for registry in registries_by_job.values():
        _assert_ready_artifact_graph(registry, registries_by_job)

    calls_before_reconcile = list(calls)
    reconciled = _run_cli(repo, "reconcile", str(workspace), env=env)
    assert set(reconciled["completed_stages"]) >= {"source_intake", "analyze", "outline", "review", "validate"}
    assert _provider_calls(counter) == calls_before_reconcile


def test_public_runtime_cli_quarantines_duplicate_and_wrong_zotero_pdfs(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "中文 Zotero quarantine"
    library = root / "Zotero 库"
    (library / "AAAA").mkdir(parents=True)
    (library / "BBBB").mkdir(parents=True)
    (library / "WRONG").mkdir(parents=True)
    duplicate_pdf = _minimal_text_pdf("DOI 10.1234/duplicate")
    (library / "AAAA" / "duplicate.pdf").write_bytes(duplicate_pdf)
    (library / "BBBB" / "duplicate.pdf").write_bytes(duplicate_pdf)
    (library / "WRONG" / "wrong-source.pdf").write_bytes(
        _minimal_text_pdf("Wrong Source DOI 10.9999/wrong.2024")
    )
    report = root / "Zotero 导出.txt"
    report.write_text(
        "\n".join(
            [
                "*",
                "Duplicate Candidate",
                "作者\tAlice Smith",
                "年份\t2024",
                "DOI\t10.1234/duplicate",
                "附件",
                "  o duplicate.pdf",
                "*",
                "Expected Source",
                "作者\tBob Jones",
                "年份\t2024",
                "DOI\t10.1234/right.2024",
                "附件",
                "  o WRONG/wrong-source.pdf",
            ]
        ),
        encoding="utf-8",
    )
    output = root / "output"
    config = root / "config.ini"
    counter = root / "provider-calls.jsonl"
    _write_config(config, output)
    spec = _write_spec(
        root / "quarantine.json",
        {
            "project_name": "synthetic-zotero-quarantine",
            "source": {
                "mode": "zotero",
                "zotero_report": str(report),
                "library_path": str(library),
            },
            "config": str(config),
            "action": "analyze",
            "queue_file": str(root / "queue.json"),
            "metadata": {"requested_stages": ["analyze"]},
        },
    )
    env = _offline_env(counter)

    result = _run_cli(
        repo,
        "run",
        str(spec),
        "--stage-handler",
        "tests.synthetic_runtime_fakes:stage_handler",
        env=env,
    )

    assert result["job_status"] == "completed"
    assert result["job_disposition"] == "needs_review"
    assert result["canonical_ready"] is False
    assert _provider_calls(counter) == []
    workspace = Path(result["workspace_path"])
    registry = _read_registry(workspace)
    source_bundle_record = _artifact_by_type(registry, "source_bundle")
    source_bundle = json.loads(Path(source_bundle_record["path"]).read_text(encoding="utf-8"))
    snapshot = source_bundle["source_snapshot"]
    assert len(snapshot["ambiguous_matches"]) == 1
    assert snapshot["ambiguous_matches"][0]["status"] == "ambiguous"
    assert len(snapshot["ambiguous_matches"][0]["candidates"]) == 2
    assert len(snapshot["quarantined_sources"]) == 1
    wrong = snapshot["quarantined_sources"][0]
    assert wrong["identity_verdict"] == "mismatch"
    assert wrong["artifact_status"] == "quarantined"
    assert wrong["expected"]["doi"] == "10.1234/right.2024"
    assert wrong["observed"]["doi"] == "10.9999/wrong.2024"
    assert source_bundle["paper_work_items"] == []
    assert [item["status"] for item in _attempt_history(workspace)] == [
        "pending",
        "running",
        "succeeded",
    ]
    _assert_latest_pointer(output, "synthetic-zotero-quarantine", result)
    _assert_ready_artifact_graph(registry, {result["job_id"]: registry})


@pytest.mark.parametrize(
    (
        "fixture_disposition",
        "cancel_stage",
        "require_clean",
        "expected_status",
        "expected_disposition",
        "expected_ready",
        "expected_attention",
    ),
    [
        ("clean", "", True, "completed", "clean", True, False),
        ("findings", "", False, "completed", "findings", True, True),
        ("needs_review", "", False, "completed", "needs_review", False, True),
        ("clean", "stage1_analyze", True, "cancelled", "unvalidated", False, True),
    ],
)
def test_public_runtime_cli_disposition_matrix(
    tmp_path: Path,
    fixture_disposition: str,
    cancel_stage: str,
    require_clean: bool,
    expected_status: str,
    expected_disposition: str,
    expected_ready: bool,
    expected_attention: bool,
) -> None:
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / f"中文 disposition {expected_disposition}"
    papers = root / "论文"
    papers.mkdir(parents=True)
    (papers / "paper-001.pdf").write_bytes(b"%PDF-1.4\n% offline fixture\n")
    output = root / "output"
    config = root / "config.ini"
    counter = root / "provider-calls.jsonl"
    _write_config(config, output)
    project_name = f"synthetic-{expected_status}-{expected_disposition}"
    spec = _write_spec(
        root / "job.json",
        {
            "project_name": project_name,
            "source": {"mode": "direct", "pdf_folder": str(papers)},
            "config": str(config),
            "action": "run_all",
            "queue_file": str(root / "queue.json"),
            "metadata": {
                "requested_stages": ["analyze", "outline", "review", "validate"],
                "validation_required": True,
                "require_clean_validation": require_clean,
            },
        },
    )
    env = _offline_env(
        counter,
        SYNTHETIC_FAST_OUTLINE="1",
        SYNTHETIC_VALIDATION_DISPOSITION=fixture_disposition,
        SYNTHETIC_CANCEL_STAGE=cancel_stage,
    )

    result = _run_cli(
        repo,
        "run",
        str(spec),
        "--stage-handler",
        "tests.synthetic_runtime_fakes:stage_handler",
        "--validator-module",
        "tests.synthetic_runtime_fakes",
        env=env,
        expected_returncodes=(1,) if expected_status == "cancelled" else (0,),
    )

    assert result["job_status"] == expected_status
    assert result["job_disposition"] == expected_disposition
    assert result["canonical_ready"] is expected_ready
    assert result["requires_attention"] is expected_attention
    workspace = Path(result["workspace_path"])
    outcome = json.loads(Path(result["job_outcome_path"]).read_text(encoding="utf-8"))
    assert outcome["job_status"] == expected_status
    assert outcome["job_disposition"] == expected_disposition
    assert outcome["canonical_ready"] is expected_ready
    terminal_status = "cancelled" if expected_status == "cancelled" else "succeeded"
    assert _attempt_history(workspace)[-1]["status"] == terminal_status
    _assert_latest_pointer(output, project_name, result)
    if expected_status == "completed":
        registry = _read_registry(workspace)
        validation = _artifact_by_type(registry, "validation_run_result")
        validation_payload = json.loads(Path(validation["path"]).read_text(encoding="utf-8"))
        assert validation_payload["validation_disposition"] == expected_disposition


def test_public_runtime_reconcile_after_report_write_crash_is_provider_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "中文 crash recovery"
    papers = root / "论文"
    papers.mkdir(parents=True)
    (papers / "paper-001.pdf").write_bytes(b"%PDF-1.4\n% offline fixture\n")
    output = root / "output"
    config = root / "config.ini"
    counter = root / "provider-calls.jsonl"
    _write_config(config, output)
    spec_path = _write_spec(
        root / "crash.json",
        {
            "project_name": "synthetic-crash",
            "job_id": "phase8-crash",
            "source": {"mode": "direct", "pdf_folder": str(papers)},
            "config": str(config),
            "action": "run_all",
            "queue_file": str(root / "queue.json"),
            "metadata": {
                "requested_stages": ["analyze", "outline", "review", "validate"],
                "validation_required": True,
                "require_clean_validation": True,
            },
        },
    )
    for key, value in {
        "SYNTHETIC_PROVIDER_COUNTER": str(counter),
        "SYNTHETIC_FAST_OUTLINE": "1",
        "SYNTHETIC_VALIDATION_DISPOSITION": "clean",
        "AUTO_GENERATE_STRICT_OFFLINE": "1",
        "PYTHONIOENCODING": "utf-8",
        "LLM_BACKUP_READER_API": "offline-fixture",
        "LLM_FREE_MODE_API": "offline-fixture",
        "LLM_OUTLINE_API": "offline-fixture",
        "LLM_PRIMARY_READER_API": "offline-fixture",
        "LLM_VALIDATOR_API": "offline-fixture",
        "LLM_WRITER_API": "offline-fixture",
    }.items():
        monkeypatch.setenv(key, value)
    injected = False

    def crash_after_report(point: str, _context: Mapping[str, Any]) -> None:
        nonlocal injected
        if point == "after_report_write_before_pointer" and not injected:
            injected = True
            raise RuntimeError("synthetic crash after report write")

    import main as legacy_main

    runner = AgentRuntimeRunner(
        load_runtime_job_spec(spec_path),
        legacy_main=legacy_main,
        stage_handler=synthetic_runtime_fakes.stage_handler,
        validator_module=synthetic_runtime_fakes,
        fault_injector=crash_after_report,
    )
    with pytest.raises(RuntimeError, match="synthetic crash after report write"):
        runner.run()

    assert injected is True
    workspace = output / "synthetic-crash__phase8-crash"
    outcome = json.loads((workspace / "artifacts" / "job_outcome_v1.json").read_text(encoding="utf-8"))
    assert outcome["job_status"] == "completed"
    assert outcome["canonical_ready"] is True
    pointer_path = output / "synthetic-crash" / "_latest_job.json"
    pointer_before = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer_before["status"] == "running"
    assert [item["status"] for item in _attempt_history(workspace)] == [
        "pending",
        "running",
        "succeeded",
    ]
    calls_before = _provider_calls(counter)
    assert [item["kind"] for item in calls_before] == ["stage1", "stage2", "stage3"]
    terminal_hashes_before = {
        path.name: file_sha256(path)
        for path in (workspace / "artifacts" / "runtime_stage_terminals").rglob("*.json")
    }

    env = _offline_env(counter)
    reconciled = _run_cli(repo, "reconcile", str(workspace), env=env)

    assert reconciled["pointer_repaired"] is True
    assert reconciled["issues"] == []
    assert set(reconciled["completed_stages"]) >= {
        "source_intake",
        "analyze",
        "outline",
        "review",
        "validate",
    }
    assert _provider_calls(counter) == calls_before
    assert {
        path.name: file_sha256(path)
        for path in (workspace / "artifacts" / "runtime_stage_terminals").rglob("*.json")
    } == terminal_hashes_before
    pointer_after = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer_after["status"] == "completed"
    registry = _read_registry(workspace)
    _assert_ready_artifact_graph(registry, {"phase8-crash": registry})


def test_public_runtime_cli_run_status_resume_and_explicit_job_collision(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "public-runtime-lifecycle"
    papers = root / "papers"
    papers.mkdir(parents=True)
    (papers / "paper-001.pdf").write_bytes(b"%PDF-1.4\n% offline fixture\n")
    output = root / "output"
    config = root / "config.ini"
    counter = root / "provider-calls.jsonl"
    _write_config(config, output)
    spec = _write_spec(
        root / "job.json",
        {
            "project_name": "synthetic-public-lifecycle",
            "source": {"mode": "direct", "pdf_folder": str(papers)},
            "config": str(config),
            "action": "analyze",
            "queue_file": str(root / "queue.json"),
            "metadata": {"requested_stages": ["analyze"]},
        },
    )
    env = _offline_env(counter)
    job_id = "phase8-public-lifecycle"
    common_args = (
        str(spec),
        "--job-id",
        job_id,
        "--stage-handler",
        "tests.synthetic_runtime_fakes:stage_handler",
    )

    first = _run_cli(repo, "run", *common_args, env=env)
    assert first["job_id"] == job_id
    assert first["job_status"] == "completed"
    assert first["attempt_number"] == 1
    workspace = Path(first["workspace_path"])

    first_status = _run_cli(repo, "status", str(workspace), env=env)
    for field in (
        "job_id",
        "job_status",
        "job_disposition",
        "canonical_ready",
        "attempt_number",
        "completed_stages",
        "workspace_path",
        "job_outcome_path",
    ):
        assert first_status[field] == first[field]

    collision = _run_cli_process(repo, "run", *common_args, env=env)
    assert collision.returncode != 0
    assert "workspace already exists" in collision.stdout + collision.stderr
    assert len(_provider_calls(counter)) == 1

    resumed = _run_cli(repo, "resume", *common_args, env=env)
    assert resumed["job_id"] == job_id
    assert resumed["job_status"] == "completed"
    assert resumed["attempt_number"] == 2
    assert resumed["resumed_from_attempt"] == 1
    assert len(_provider_calls(counter)) == 1

    final_status = _run_cli(repo, "status", str(workspace), env=env)
    for field in (
        "job_id",
        "job_status",
        "job_disposition",
        "canonical_ready",
        "attempt_number",
        "completed_stages",
        "workspace_path",
        "job_outcome_path",
    ):
        assert final_status[field] == resumed[field]
    assert [item["status"] for item in _attempt_history(workspace)] == [
        "pending",
        "running",
        "succeeded",
        "pending",
        "running",
        "succeeded",
    ]
    _assert_latest_pointer(output, "synthetic-public-lifecycle", resumed)
