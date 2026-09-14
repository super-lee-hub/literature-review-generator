from __future__ import annotations

import configparser
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import time
from typing import Any

import fitz  # type: ignore
import pytest

from runtime.provider_runtime import ProviderAggregateBudgetV1
from runtime.release_acceptance import (
    AcceptanceScenarioContextV1,
    GateEvidenceProducer,
    GateEvidenceVerifier,
    GateIScenario,
)
from summary_schema import normalize_ai_summary


pytestmark = [pytest.mark.integration, pytest.mark.playwright]

REPO_ROOT = Path(__file__).resolve().parents[1]
FINAL_SHA = "9fcbf04f5399147ea5602e79968a5465987482e3"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _canonical_summary() -> dict[str, Any]:
    return normalize_ai_summary(
        {
            "routing": {
                "paper_type": "empirical",
                "paper_subtype_raw": "experiment",
                "paper_subtype_normalized": "experiment",
                "classification_status": "resolved",
                "route_confidence": "high",
                "classification_rationale": "controlled empirical study",
                "secondary_candidates": [],
            },
            "paper_metadata": {
                "title": "Local GUI acceptance study",
                "authors": ["Local Tester"],
                "year": "2026",
                "journal": "Integration Journal",
                "doi": "10.1000/local-gui",
            },
            "core_analysis": {
                "summary": "The study reports a controlled test of a local acceptance workflow.",
                "key_points": ["The treatment improved the measured outcome."],
                "methodology": "A controlled experiment with 120 observations.",
                "findings": "The treatment improved the outcome by 15 percent with p below 0.01.",
                "conclusions": "The local acceptance workflow reached the expected conclusion.",
                "relevance": "The result exercises the Stage 1 production path.",
                "limitations": "The provider endpoint is local and is not a live research result.",
                "theoretical_framework": None,
                "research_gap": "External replication remains required.",
                "future_research_directions": ["Run the authoritative F1 corpus."],
            },
            "specialized_details": {
                "empirical": {
                    "research_questions_or_hypotheses": ["Does the treatment improve the outcome?"],
                    "data_source_and_size": "Controlled experiment, N=120.",
                    "analysis_technique": "Group comparison with significance testing.",
                    "core_variables": {
                        "independent": ["treatment"],
                        "dependent": ["outcome"],
                    },
                    "sample_characteristics_or_context": "Controlled local test context.",
                },
                "review": None,
                "conceptual": None,
            },
        }
    )


class _LocalSummaryProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                size = int(self.headers.get("Content-Length", "0"))
                owner.requests.append(
                    {
                        "path": self.path,
                        "payload": json.loads(self.rfile.read(size).decode("utf-8")),
                    }
                )
                response = {
                    "model": "local-stage1",
                    "usage": {
                        "prompt_tokens": 64,
                        "completion_tokens": 48,
                        "total_tokens": 112,
                    },
                    "choices": [
                        {
                            "message": {"content": json.dumps(_canonical_summary())},
                            "finish_reason": "stop",
                        }
                    ],
                }
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> "_LocalSummaryProvider":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def _write_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Local GUI acceptance study\n\n"
        "Abstract: This controlled experiment evaluates a treatment with 120 observations.\n\n"
        "Methodology: Participants were assigned to treatment and control conditions.\n\n"
        "Results: The treatment improved the outcome by 15 percent (p < 0.01).\n\n"
        "Conclusion: The controlled workflow supports the stated mechanism.",
    )
    document.save(path)
    document.close()


def _write_config(config_path: Path, output_root: Path, provider_url: str) -> None:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(output_root)
    parser["Primary_Reader_API"].update(
        {
            "api_key": "loaded_from_.env_file",
            "model": "local-stage1",
            "api_base": provider_url,
            "provider_family": "generic",
            "endpoint_type": "chat_completions",
            "proxy_mode": "direct",
            "max_output_tokens": "512",
            "transport_retries": "0",
        }
    )
    parser["Stage1_Input"].update(
        {
            "primary_reader_only": "true",
            "send_selected_visuals": "false",
            "stage1_synthesis_max_output_tokens": "512",
            "stage1_length_retry_max_attempts": "0",
            "stage1_semantic_retry_max_attempts": "0",
        }
    )
    parser["Stage1_Visual"]["enabled"] = "false"
    parser["Preprocess"].update(
        {
            "cache_dir": str(output_root / "preprocess-cache"),
            "parser_mode": "local",
            "primary_parser": "local",
            "fallback_parser": "none",
            "force_rebuild": "true",
        }
    )
    parser["Validation"]["review_enabled"] = "false"
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    (config_path.parent / ".env").write_text(
        "LLM_PRIMARY_READER_API=local-stage1-key\n",
        encoding="utf-8",
    )


def test_gate_i_production_gui_local_provider_is_real_io_but_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTO_GENERATE_ENV_PATH", raising=False)
    output_root = tmp_path / "output"
    pdf_folder = tmp_path / "pdfs"
    pdf_folder.mkdir()
    _write_pdf(pdf_folder / "study.pdf")

    with _LocalSummaryProvider() as provider:
        config_path = tmp_path / "config.ini"
        _write_config(config_path, output_root, provider.base_url)
        gui_port = _pick_free_port()
        acceptance_run_id = "local-gate-i-run"
        input_manifest = tmp_path / "gate-i-input.json"
        input_manifest.write_text(
            json.dumps(
                {
                    "artifact_type": "acceptance_gui_input",
                    "artifact_version": "v2",
                    "schema_version": "acceptance-gui-input-v2",
                    "execution_kind": "production",
                    "base_url": f"http://127.0.0.1:{gui_port}",
                    "config_path": str(config_path),
                    "repo_root": str(REPO_ROOT),
                    "output_root": str(output_root),
                    "input_mode": "pdf",
                    "pdf_folder": str(pdf_folder),
                    "zotero_report": "",
                    "library_path": "",
                    "project_name": "local-gate-i",
                    "work_mode": "normal",
                    "action": "analyze",
                    "port": gui_port,
                    "startup_timeout_seconds": 45,
                    "completion_timeout_seconds": 180,
                }
            ),
            encoding="utf-8",
        )
        evidence_root = tmp_path / "acceptance-evidence"
        context = AcceptanceScenarioContextV1(
            acceptance_run_id=acceptance_run_id,
            final_executable_sha=FINAL_SHA,
            runtime_spec_path="",
            workspace_path="",
            job_id="",
            evidence_root=str(evidence_root),
            process_event_log=str(evidence_root / "process_events.jsonl"),
            owner_authorized=True,
            provider_budget=ProviderAggregateBudgetV1(
                max_provider_calls_total=4,
                max_output_tokens_total=4_000,
                max_retry_attempts_total=1,
                max_wall_seconds=180,
            ).to_dict(),
            provider_budget_state_path=str(evidence_root / "provider_budget.json"),
            scenario_execution_receipt_path=str(evidence_root / "I" / "scenario_execution_receipt.json"),
            input_manifest_path=str(input_manifest),
            absolute_deadline_epoch=time.time() + 180,
        )

        result = GateIScenario().execute(context, (), runtime_result=None)

    assert result.status == "READY_FOR_SEMANTIC_VERIFICATION", result.reason
    assert len(provider.requests) >= 1
    assert all(request["path"] == "/v1/chat/completions" for request in provider.requests)
    job_ids = {str(ref.get("job_id") or "") for ref in result.evidence_refs}
    assert len(job_ids) == 1
    job_id = job_ids.pop()
    assert job_id
    evidence = GateEvidenceProducer(final_sha=FINAL_SHA).build_gate(
        "I",
        result.evidence_refs,
        acceptance_run_id=acceptance_run_id,
        scenario_id="I",
        job_id=job_id,
    )
    verdict = GateEvidenceVerifier().verify(
        "I",
        evidence,
        expected_final_sha=FINAL_SHA,
        expected_acceptance_run_id=acceptance_run_id,
        expected_job_id=job_id,
    )

    assert verdict["status"] == "PASS_OFFLINE", verdict
    assert verdict["derived_facts"]["actual_transport_calls"] >= 1
    assert verdict["derived_facts"]["nonlocal_transport_calls"] == 0
    for ref in result.evidence_refs:
        if ref.get("role") in {
            "browser_evidence",
            "playwright_trace",
            "playwright_screenshot_manifest",
        }:
            assert Path(str(ref["path"])).parent.name == "acceptance_gui"
