"""A partial Stage 1 resumes through the CLI without repeating successful HTTP work."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

import ai_interface
from reviewctl import main
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import ProviderRuntimeLedger, hash_json
from runtime.runner import AgentRuntimeRunner
from services.artifact_registry import file_sha256
from services.stage1_analysis_service import Stage1AnalysisService
from tests.test_run_first_scale_integration import (
    _local_config, _scale_papers, _start_scale_server, _write_scale_pdf,
)


class _PowerLoss(BaseException):
    """Fault injection that cannot be converted into a completed stage terminal."""


@pytest.mark.integration
@pytest.mark.parametrize("tampered_raw", [False, True], ids=["known-success", "tampered-raw"])
def test_cli_resumes_partial_stage1_without_reposting_successful_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tampered_raw: bool,
) -> None:
    papers = _scale_papers(3)
    pdfs = tmp_path / "papers"
    pdfs.mkdir()
    for paper in papers:
        _write_scale_pdf(pdfs / f"{paper.stem}.pdf", paper)
    source_hashes = {str(path): file_sha256(path) for path in pdfs.glob("*.pdf")}
    server, thread, api_base = _start_scale_server(papers)
    try:
        config = _local_config(tmp_path, api_base, provider_call_limit=4)
        config_hash = file_sha256(config)
        origin = urlsplit(api_base)
        original_send = requests.Session.send

        def local_only(session, request, **kwargs):
            target = urlsplit(request.url)
            assert (target.scheme, target.hostname, target.port) == (
                origin.scheme, origin.hostname, origin.port,
            ), "partial recovery attempted a nonfixture HTTP request"
            return original_send(session, request, **kwargs)

        monkeypatch.setattr(requests.Session, "send", local_only)
        spec = RuntimeJobSpec(
            project_name="partial-stage1", job_id="partial-stage1-http", action="analyze",
            source=RuntimeSourceSpec(mode="direct", pdf_folder=str(pdfs)),
            config=str(config), queue_file=str(tmp_path / "queue.json"),
            metadata={"provider_budget": {
                "schema_version": "provider-aggregate-budget-contract-v2",
                "max_provider_calls_total": 4, "max_output_tokens_total": 256_000,
                "max_retry_attempts_total": 0, "max_wall_seconds": 600,
            }},
        )
        original_execute = Stage1AnalysisService._execute_prepared
        completed_calls = 0

        def lose_power_before_next_call(self, *args, **kwargs):
            nonlocal completed_calls
            if completed_calls == 1:
                raise _PowerLoss("simulate loss between completed Reader calls")
            result = original_execute(self, *args, **kwargs)
            completed_calls += 1
            return result

        monkeypatch.setattr(Stage1AnalysisService, "_execute_prepared", lose_power_before_next_call)
        with pytest.raises(_PowerLoss):
            AgentRuntimeRunner(spec).run()
        monkeypatch.setattr(Stage1AnalysisService, "_execute_prepared", original_execute)
        assert len(server.calls) == 1
        workspace_path = tmp_path / "output" / "partial-stage1__partial-stage1-http"
        workspace, registry = AgentRuntimeRunner._open_workspace(workspace_path)
        graph_record = registry.get("stage1:provider_expected_call_graph")
        assert graph_record is not None
        graph = json.loads(Path(graph_record.path).read_text(encoding="utf-8"))
        assert graph["attempt_id"]
        staged = Path(workspace.artifact_path(
            f".publication-staging/provider-receipts/stage1/{graph['attempt_id']}.jsonl"
        ))
        original_receipt_bytes = staged.read_bytes()
        old_receipts = ProviderRuntimeLedger(staged).list_receipts()
        assert len(old_receipts) == 1 and old_receipts[0].status == "success"
        assert old_receipts[0].closure_epoch_id == graph["closure_epoch_id"]
        assert registry.get("stage1_provider_receipts") is None
        assert registry.get("stage1:provider_receipt_closure") is None
        budget_path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
        budget_before = json.loads(budget_path.read_text(encoding="utf-8"))
        identity_hash = file_sha256(str(budget_path) + ".identity.json")
        assert budget_before["calls_used"] == 1
        assert budget_before["calls_reserved"] == 0 and budget_before["reservations"] == []
        assert budget_before["retry_attempts_used"] == 0
        original_reparse = ai_interface.reparse_provider_response_spool
        reparsed_receipts = []

        def track_original_reparse(*args, **kwargs):
            parsed = original_reparse(*args, **kwargs)
            if str(args[0]) == str(old_receipts[0].metadata["raw_response_path"]):
                assert str(args[1]) == old_receipts[0].metadata["raw_response_sha256"]
                assert int(args[2]) == old_receipts[0].metadata["response_bytes"]
                assert hash_json(parsed["content"]) == old_receipts[0].response_hash
                reparsed_receipts.append(old_receipts[0].receipt_id)
            return parsed

        monkeypatch.setattr(ai_interface, "reparse_provider_response_spool", track_original_reparse)
        if tampered_raw:
            raw_path = Path(str(old_receipts[0].metadata["raw_response_path"]))
            raw_path.write_bytes(raw_path.read_bytes() + b"tampered")
        capsys.readouterr()
        args = [
            "--repo-root", str(Path(__file__).resolve().parents[1]), "--config", str(config),
            "resume", "--workspace", str(workspace_path), "--json",
        ]
        exit_code = main(args)
        output = capsys.readouterr().out
        if tampered_raw:
            assert exit_code == 1, output
            rejected = json.loads(output.strip().splitlines()[-1])
            assert rejected["job_status"] == "failed", rejected
            assert "STAGE1_PUBLICATION_RECOVERY_BLOCKED" in rejected["message"], rejected
            assert len(server.calls) == 1, "rejected replay sent a pending Reader request"
            assert staged.read_bytes() == original_receipt_bytes
            rejected_budget = json.loads(budget_path.read_text(encoding="utf-8"))
            # Binding a new Runtime controller rotates its fencing owner UUID
            # (ProviderBudgetController._persist_state_unlocked). Every quota,
            # counter, reservation and clock field must still be identical.
            assert rejected_budget["owner_id"] != budget_before["owner_id"]
            assert {key: value for key, value in rejected_budget.items() if key != "owner_id"} == {
                key: value for key, value in budget_before.items() if key != "owner_id"
            }
            assert file_sha256(str(budget_path) + ".identity.json") == identity_hash
            assert file_sha256(config) == config_hash
            assert {name: file_sha256(name) for name in source_hashes} == source_hashes
            assert not server.errors and not server.transport_errors
            return
        assert exit_code == 0, output
        resumed = json.loads(output.strip().splitlines()[-1])
        assert resumed["job_status"] == "completed", resumed
        assert "analyze" in resumed["completed_stages"], resumed
        assert len(server.calls) == 3, "successful pre-interruption Reader was posted again"
        assert old_receipts[0].receipt_id in reparsed_receipts, "retained raw response was not reparsed"
        assert all(row["stage"] == "stage1_reader" for row in server.calls)
        assert staged.read_bytes().startswith(original_receipt_bytes), "original receipts were rewritten"
        registry.reload()
        ledger_record = registry.get("stage1_provider_receipts")
        closure_record = registry.get("stage1:provider_receipt_closure")
        assert ledger_record is not None and closure_record is not None
        registry.verify_ready_artifact_closure(closure_record)
        receipts = ProviderRuntimeLedger(ledger_record.path).list_receipts()
        assert len(receipts) == 3
        assert receipts[0].to_dict() == old_receipts[0].to_dict()
        assert {r.attempt_id for r in receipts} == {graph["attempt_id"]}
        assert {r.closure_epoch_id for r in receipts} == {graph["closure_epoch_id"]}
        summary_record = registry.get("summary_file")
        assert summary_record is not None and summary_record.status == "ready"
        registry.verify_ready_artifact_closure(summary_record)
        summary_rows = json.loads(Path(summary_record.path).read_text(encoding="utf-8"))
        assert isinstance(summary_rows, list) and len(summary_rows) == 3
        assert {row["paper_info"]["canonical_paper_key"] for row in summary_rows} == {
            paper.paper_key for paper in papers
        }
        budget_after = json.loads(budget_path.read_text(encoding="utf-8"))
        assert budget_after["budget"] == budget_before["budget"]
        assert budget_after["first_started_epoch"] == budget_before["first_started_epoch"]
        assert budget_after["absolute_deadline_epoch"] == budget_before["absolute_deadline_epoch"]
        assert budget_after["calls_used"] == 3 and budget_after["calls_reserved"] == 0
        assert budget_after["retry_attempts_used"] == 0 and budget_after["reservations"] == []
        assert file_sha256(str(budget_path) + ".identity.json") == identity_hash
        assert file_sha256(config) == config_hash
        assert {name: file_sha256(name) for name in source_hashes} == source_hashes
        assert main(args) == 0
        replayed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert replayed["job_status"] == "completed", replayed
        assert len(server.calls) == 3
        registry.reload()
        assert registry.get("summary_file") == summary_record
        assert file_sha256(summary_record.path) == summary_record.content_hash
        assert not server.errors and not server.transport_errors
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
