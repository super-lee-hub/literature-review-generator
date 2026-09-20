from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from config_loader import load_config
from free_mode.service import _normalize_planner_response
from preprocess.service import (
    MineruArtifactLimitError,
    MineruBudgetExceeded,
    MineruRemoteBudget,
    MineruSubmissionUncertainError,
    PreprocessManager,
)
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.trust_admission import (
    ExternalHostAdmissionError,
    _is_local_host,
    acknowledgement_from_values,
    build_external_host_policy,
    validate_external_host_acknowledgement,
)
from runtime.release_acceptance import DocumentModalityProfileV1, DocumentModalityProfileV2
from services.configuration_service import ensure_config_sections
from services.queue_service import PersistentQueueService, QueueJobSpec, QueueInputDriftError
from services.stage1_analysis_service import Stage1AnalysisService


def _review_config(api_base: str = "https://writer.example.test/v1") -> dict[str, dict[str, str]]:
    return {
        "Application": {"config_schema": "4"},
        "Paths": {"output_path": "output"},
        "Writer_API": {
            "api_key": "writer-test",
            "model": "gpt-5.6-sol",
            "api_base": api_base,
            "endpoint_type": "responses",
            "provider_family": "openai_responses",
        },
        "Validation": {"review_enabled": "false"},
    }


def _policy(config: dict[str, dict[str, str]]):
    from runtime.provider_routes import build_reachable_provider_route_plan

    plan = build_reachable_provider_route_plan(
        config,
        action="generate_review",
        requested_stages=("review",),
    )
    return build_external_host_policy(config, plan)


def test_a01_fingerprint_binds_full_transport_endpoint_and_ack_expiry() -> None:
    first = _policy(_review_config("https://writer.example.test/v1"))
    changed_path = _policy(_review_config("https://writer.example.test/other"))
    changed_port = _policy(_review_config("https://writer.example.test:9443/v1"))
    assert first.route_fingerprint != changed_path.route_fingerprint
    assert first.route_fingerprint != changed_port.route_fingerprint

    with pytest.raises(ExternalHostAdmissionError, match="expired"):
        validate_external_host_acknowledgement(
            first,
            acknowledgement_from_values(
                first,
                acknowledged=True,
                hosts=list(first.required_hosts),
                issued_at="2020-01-01T00:00:00Z",
                expires_at="2020-01-02T00:00:00Z",
            ),
        )
    with pytest.raises(ExternalHostAdmissionError, match="future"):
        validate_external_host_acknowledgement(
            first,
            acknowledgement_from_values(
                first,
                acknowledged=True,
                hosts=list(first.required_hosts),
                issued_at="2999-01-01T00:00:00Z",
                expires_at="2999-01-02T00:00:00Z",
            ),
        )
    fresh = acknowledgement_from_values(first, acknowledged=True, hosts=list(first.required_hosts))
    fresh["unexpected"] = True
    with pytest.raises(ExternalHostAdmissionError, match="unknown fields"):
        validate_external_host_acknowledgement(first, fresh)


@pytest.mark.parametrize(
    "value",
    ["::1", "[::1]", "http://[::1]:8080", "127.0.0.1", "localhost"],
)
def test_g02_local_host_parser_handles_ipv6_and_urls(value: str) -> None:
    assert _is_local_host(value) is True


@pytest.mark.parametrize("value", ["2001:db8::1", "localhost.evil", "https://127.0.0.1:bad"])
def test_g02_local_host_parser_does_not_overmatch(value: str) -> None:
    assert _is_local_host(value) is False


def test_a02_manager_and_admission_share_effective_mineru_hosts() -> None:
    config = _review_config("https://api.openai.com/v1")
    config["Preprocess"] = {
        "parser_mode": "remote",
        "primary_parser": "mineru_remote",
        "mineru_base_url": "https://mineru.example.test/api/v4",
        "mineru_allowed_url_hosts": "custom.example.test",
    }
    policy = _policy(config)
    manager = PreprocessManager(config=config, preprocess_environment_resolved=True)
    assert set(policy.required_hosts) >= set(manager.mineru_allowed_url_hosts)
    assert "custom.example.test" in manager.mineru_allowed_url_hosts
    assert "mineru.oss-cn-shanghai.aliyuncs.com" in manager.mineru_allowed_url_hosts
    assert "cdn-mineru.openxlab.org.cn" in manager.mineru_allowed_url_hosts


def test_a03_ordinary_offline_environment_does_not_skip_runner_admission(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "\n".join(
            (
                "[Application]",
                "config_schema = 4",
                "[Paths]",
                f"output_path = {tmp_path / 'output'}",
                "[Primary_Reader_API]",
                "api_key = reader-test",
                "model = reader-test",
                "api_base = https://reader.example.test/v1",
                "endpoint_type = responses",
                "provider_family = openai_responses",
                "[Stage1_Input]",
                "primary_reader_only = true",
            )
        ),
        encoding="utf-8",
    )
    spec = RuntimeJobSpec(
        project_name="a03",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path)),
        config=str(config_path),
        action="analyze",
        metadata={"requested_stages": ["analyze"]},
    )
    script = "\n".join(
        (
            "from runtime.runner import AgentRuntimeRunner, RuntimeRunnerError",
            "from runtime.job_spec import RuntimeJobSpec",
            "import json,sys",
            "spec=RuntimeJobSpec.from_dict(json.loads(open(sys.argv[1], encoding='utf-8').read()))",
            "try:",
            "    AgentRuntimeRunner(spec)._normalized_spec(resume=False)",
            "except RuntimeRunnerError as exc:",
            "    print(str(exc))",
            "    raise SystemExit(0 if 'external host' in str(exc) else 2)",
            "else:",
            "    raise SystemExit(3)",
        )
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")
    child_env = dict(os.environ)
    child_env["AUTO_GENERATE_OFFLINE_TESTS"] = "1"
    child_env.pop("AUTO_GENERATE_ACCEPTANCE_CONTEXT_JSON", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(spec_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_b01_stage1_does_not_reopen_quality_blocked_text(tmp_path: Path, monkeypatch) -> None:
    generation = tmp_path / "cache" / "generation-1"
    generation.mkdir(parents=True)
    (generation / "prepare_manifest.json").write_text("{}", encoding="utf-8")
    result = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        manifest_path=str(generation / "prepare_manifest.json"),
        stage1_quality_reasons=["too_short"],
        stage1_quality_level="BLOCK",
        stage1_input_text="",
        plain_text="x",
        markdown_text="x",
        page_index=[{"page_number": 1}],
    )
    released: list[dict[str, str]] = []

    class FakeManager:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare_pdf(self, _source_pdf: str, **_kwargs):
            return result

        def release_generation_lease(self, _cache_dir: str, **kwargs: str) -> int:
            released.append(dict(kwargs))
            return 1

    service = object.__new__(Stage1AnalysisService)
    service.job_id = "b01-job"
    service.attempt_id = "b01-attempt"
    service.config = {}
    service.logger = None
    from services.job_workspace import JobWorkspace

    service.workspace = JobWorkspace.create(str(tmp_path / "output"), "b01", "job")
    monkeypatch.setattr("services.stage1_analysis_service.PreprocessManager", FakeManager)
    with pytest.raises(RuntimeError, match="incomplete"):
        with service._preprocess("source.pdf", paper_key="paper"):
            raise AssertionError("quality-blocked input must not be yielded")
    assert released


def test_b02_snapshot_rejects_changed_leaf_against_published_manifest(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    doc = __import__("fitz").open()
    doc.new_page().insert_text((72, 72), "Complete source text. " * 100)
    doc.save(pdf)
    doc.close()
    manager = PreprocessManager(
        config={
            "Paths": {"output_path": str(tmp_path)},
            "Preprocess": {
                "cache_dir": str(tmp_path / "cache"),
                "extractor_profile": "fitz",
                "ocr_mode": "off",
            },
        }
    )
    prepared = manager.prepare_pdf(str(pdf))
    assert prepared is not None
    Path(prepared.stage1_input_path).write_text("tampered", encoding="utf-8")
    service = object.__new__(Stage1AnalysisService)
    from services.job_workspace import JobWorkspace

    service.workspace = JobWorkspace.create(str(tmp_path / "output"), "b02", "job")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        service._snapshot_preprocess_authority(prepared, paper_key="paper")


def test_c01_mineru_budget_is_cross_process_durable(tmp_path: Path) -> None:
    state = tmp_path / "mineru-budget.json"
    first = MineruRemoteBudget(state_path=state)
    first.bind_limits(max_tasks=1, max_http_calls=2, max_upload_bytes=10)
    first.reserve_task(reservation_id="task-1")
    second = MineruRemoteBudget(state_path=state)
    second.bind_limits(max_tasks=1, max_http_calls=2, max_upload_bytes=10)
    with pytest.raises(MineruBudgetExceeded):
        second.reserve_task(reservation_id="task-2")
    second.reserve_http_call(upload_bytes=4, reservation_id="http-1")
    assert second.snapshot()["tasks_used"] == 1
    assert second.snapshot()["upload_bytes_used"] == 4


def test_c01_pending_marker_reconciles_a_reservation_before_post(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"stable-pdf")
    trace_path = tmp_path / "trace.json"
    budget_path = tmp_path / "budget.json"
    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "parser_mode": "remote",
            "primary_parser": "mineru_remote",
            "fallback_parser": "local",
            "mineru_base_url": "https://mineru.example.test/api/v4",
            "mineru_api_token": "token",
            "cache_dir": str(tmp_path / "cache"),
        },
    }
    first = PreprocessManager(
        config=config,
        preprocess_environment_resolved=True,
        mineru_budget_state_path=budget_path,
        mineru_trace_state_path=trace_path,
    )
    first._begin_mineru_trace(pdf_path=str(pdf), request_payload={"x": 1})
    pending = json.loads(trace_path.read_text(encoding="utf-8"))
    pending["private"]["budget_reserved"] = False
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    budget["tasks_used"] = 0
    budget["reservations"] = {}
    trace_path.write_text(json.dumps(pending), encoding="utf-8")
    budget_path.write_text(json.dumps(budget), encoding="utf-8")

    resumed = PreprocessManager(
        config=config,
        preprocess_environment_resolved=True,
        mineru_budget_state_path=budget_path,
        mineru_trace_state_path=trace_path,
    )
    resumed._begin_mineru_trace(pdf_path=str(pdf), request_payload={"x": 1})

    assert resumed.mineru_budget.snapshot()["tasks_used"] == 1
    assert json.loads(trace_path.read_text(encoding="utf-8"))["private"]["budget_reserved"] is True


def test_c01_unknown_mineru_post_is_persisted_and_not_retried(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"stable-pdf")
    trace_path = tmp_path / "trace.json"
    budget_path = tmp_path / "budget.json"
    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "parser_mode": "remote",
            "primary_parser": "mineru_remote",
            "fallback_parser": "local",
            "mineru_base_url": "https://mineru.example.test/api/v4",
            "mineru_api_token": "token",
            "cache_dir": str(tmp_path / "cache"),
        },
    }
    manager = PreprocessManager(
        config=config,
        preprocess_environment_resolved=True,
        mineru_budget_state_path=budget_path,
        mineru_trace_state_path=trace_path,
    )
    manager._begin_mineru_trace(pdf_path=str(pdf), request_payload={"x": 1})
    manager._reserve_mineru_transport(kind="json_post", url=manager.mineru_base_url)
    assert json.loads(trace_path.read_text(encoding="utf-8"))["private"]["post_sent"] is True
    resumed = PreprocessManager(
        config=config,
        preprocess_environment_resolved=True,
        mineru_budget_state_path=budget_path,
        mineru_trace_state_path=trace_path,
    )
    with pytest.raises(MineruSubmissionUncertainError):
        resumed._begin_mineru_trace(pdf_path=str(pdf), request_payload={"x": 1})


def test_c02_transport_event_overflow_does_not_mutate_event_64(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"stable-pdf")
    manager = PreprocessManager(
        config={"Paths": {"output_path": str(tmp_path)}, "Preprocess": {"cache_dir": str(tmp_path / "cache")}}
    )
    manager._begin_mineru_trace(pdf_path=str(pdf), request_payload={"x": 1})
    for index in range(65):
        manager._reserve_mineru_transport(kind="json_get", url="https://mineru.example.test/status")
        manager._record_mineru_transport_result(status_code=200 + index)
    trace = manager._active_mineru_trace
    assert trace is not None
    assert len(trace["transport_events"]) == 64
    assert trace["transport_events"][-1]["status_code"] == 263
    assert trace["transport_events_truncated"] == 1


def test_d01_json_response_is_streamed_and_bounded(monkeypatch) -> None:
    manager = PreprocessManager(config={"Preprocess": {"cache_dir": "cache"}})
    manager.mineru_base_url = "https://mineru.example.test/api/v4"
    manager.mineru_api_token = "token"
    observed: dict[str, object] = {}

    class Response:
        status_code = 200
        headers: dict[str, str] = {}
        closed = False

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, *, chunk_size: int):
            observed["chunk_size"] = chunk_size
            return [b'{"ok":', b"true}"]

        def close(self) -> None:
            self.closed = True

    def request(**kwargs):
        observed.update(kwargs)
        response = Response()
        observed["response"] = response
        return response

    monkeypatch.setattr("preprocess.service.requests.request", request)
    assert manager._request_json("get", manager.mineru_base_url + "/status") == {"ok": True}
    assert observed["stream"] is True
    assert observed["response"].closed is True  # type: ignore[union-attr]

    manager.mineru_json_max_bytes = 4
    with pytest.raises(MineruArtifactLimitError):
        manager._request_json("get", manager.mineru_base_url + "/status")


def test_d02_upload_uses_frozen_bytes_after_source_replacement(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    original = b"0123456789"
    pdf.write_bytes(original)
    manager = PreprocessManager(
        config={
            "Paths": {"output_path": str(tmp_path)},
            "Preprocess": {
                "cache_dir": str(tmp_path / "cache"),
                "parser_mode": "remote",
                "primary_parser": "mineru_remote",
                "fallback_parser": "local",
                "mineru_base_url": "https://mineru.example.test/api/v4",
                "mineru_api_token": "token",
            },
        },
        preprocess_environment_resolved=True,
    )
    monkeypatch.setattr(
        manager,
        "_request_json",
        lambda *_args, **_kwargs: {
            "batch_id": "batch-1",
            "upload_urls": ["https://mineru.oss-cn-shanghai.aliyuncs.com/upload"],
        },
    )
    monkeypatch.setattr(manager, "_poll_mineru_result", lambda **kwargs: kwargs["seed_payload"])
    uploaded: list[bytes] = []

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    def put(_url: str, *, data: bytes, **_kwargs):
        uploaded.append(bytes(data))
        pdf.write_bytes(b"replacement" * 20)
        return Response()

    monkeypatch.setattr("preprocess.service.requests.put", put)
    manager._extract_with_mineru_remote(str(pdf), [], [], [])
    assert uploaded == [original]


def test_e01_normal_load_chain_applies_all_six_mineru_environment_values(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.ini"
    config = ensure_config_sections({})
    config["Paths"]["output_path"] = str(tmp_path / "output")
    config["Preprocess"]["parser_mode"] = "local"
    config["Preprocess"]["primary_parser"] = "local"
    parser = configparser.ConfigParser()
    parser.optionxform = str
    for section, values in config.items():
        parser[section] = values
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    values = {
        "MINERU_REQUEST_TIMEOUT_SECONDS": "11",
        "MINERU_UPLOAD_TIMEOUT_SECONDS": "12",
        "MINERU_DOWNLOAD_TIMEOUT_SECONDS": "13",
        "MINERU_MAX_REMOTE_TASKS": "14",
        "MINERU_MAX_REMOTE_HTTP_CALLS": "15",
        "MINERU_MAX_REMOTE_UPLOAD_BYTES": "16",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("config_loader.validate_all_config", lambda _config, **_kwargs: (True, []))
    loaded = load_config(str(config_path))
    manager = PreprocessManager(
        loaded,
        preprocess_environment_resolved=loaded.preprocess_environment_resolved,
    )
    assert manager.mineru_request_timeout_seconds == 11
    assert manager.mineru_upload_timeout_seconds == 12
    assert manager.mineru_download_timeout_seconds == 13
    assert manager.mineru_max_remote_tasks == 14
    assert manager.mineru_max_remote_http_calls == 15
    assert manager.mineru_max_remote_upload_bytes == 16


def test_e02_queue_freezes_effective_settings_and_rejects_process_env_drift(tmp_path: Path, monkeypatch) -> None:
    config = ensure_config_sections({})
    config["Paths"]["output_path"] = str(tmp_path / "output")
    config["Stage1_Input"]["primary_reader_only"] = "true"
    config["Primary_Reader_API"]["api_key"] = "reader-key"
    config_path = tmp_path / "config.ini"
    parser = configparser.ConfigParser()
    parser.optionxform = str
    for section, values in config.items():
        parser[section] = values
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    monkeypatch.delenv("LLM_PRIMARY_READER_API", raising=False)
    queue = PersistentQueueService(tmp_path / "queue.json")
    spec = RuntimeJobSpec(
        project_name="queue-effective",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path)),
        job_id="queue-effective-job",
        config=str(config_path),
        action="analyze",
        queue_file=str(tmp_path / "queue.json"),
        metadata={"requested_stages": ["analyze"]},
    )
    queue.add_job(
        QueueJobSpec(
            job_id=spec.job_id,
            job_type="analyze",
            project_name=spec.project_name,
            parameters=spec.to_dict(),
        )
    )
    record = queue.get_job(spec.job_id)
    assert record is not None
    assert record.execution_snapshot["effective_runtime"]["status"] == "ready"
    assert "reader-key" not in json.dumps(record.execution_snapshot)
    monkeypatch.setenv("LLM_PRIMARY_READER_API", "changed-reader-key")
    with pytest.raises(QueueInputDriftError, match="changed"):
        queue.execution_runtime_spec(spec.job_id)


def test_f01_config_loader_diagnostic_never_prints_mineru_token(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "\n".join(
            (
                "[Application]",
                "config_schema = 4",
                "[Paths]",
                "output_path = output",
                "[Primary_Reader_API]",
                "api_key = provider-canary",
                "model = reader",
                "api_base = https://reader.example.test/v1",
                "[Backup_Reader_API]",
                "api_key = provider-canary",
                "model = backup",
                "api_base = https://reader.example.test/v1",
                "[Writer_API]",
                "api_key = provider-canary",
                "model = writer",
                "api_base = https://reader.example.test/v1",
                "[Preprocess]",
                "mineru_api_token = MINERU_CANARY_TOKEN",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "config_loader"],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        text=True,
        capture_output=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "MINERU_CANARY_TOKEN" not in combined
    assert "provider-canary" not in combined


@pytest.mark.parametrize("value", ["true", "false", "0", "", 1, 0, None])
def test_g01_ready_to_apply_accepts_only_json_boolean(value: object) -> None:
    result = _normalize_planner_response({"ready_to_apply": value})
    expected = value if isinstance(value, bool) else False
    assert result["ready_to_apply"] is expected


def test_modality_requires_ocr_or_scan_primary_page_coverage() -> None:
    v1 = DocumentModalityProfileV1(
        source_pdf_sha256="a" * 64,
        total_page_count=21,
        text_page_ratio=20 / 21,
        image_page_ratio=1 / 21,
        table_count=0,
        figure_count=0,
        scanned_candidate_page_count=1,
        ocr_used_page_count=1,
        selected_visual_count=0,
        extractor_used="pymupdf-deterministic-profile",
    )
    v2 = DocumentModalityProfileV2(
        source_pdf_sha256="b" * 64,
        preprocess_manifest_hash="c" * 64,
        stage1_input_manifest_hash="d" * 64,
        actual_extractor="pymupdf",
        page_count=21,
        text_page_count=20,
        image_page_count=1,
        table_count=0,
        figure_count=0,
        scanned_candidate_pages=1,
        actual_ocr_pages=1,
        actual_selected_visual_count=0,
        stage1_input_mode="normalized_markdown",
    )

    assert v1.derived_modality == "text_heavy"
    assert v2.derived_modality == "text_heavy"
