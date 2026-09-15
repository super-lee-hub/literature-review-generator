from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
import requests

from preprocess.provider_circuit import ProviderCircuitBreaker, ProviderCircuitOpen
from preprocess.service import (
    MineruConfigurationError,
    MineruSubmissionUncertainError,
    PreprocessManager,
)
from summary_schema import normalize_ai_summary


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _SilentLogger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def success(self, *_args, **_kwargs) -> None:
        pass


def _remote_manager(monkeypatch: pytest.MonkeyPatch, **config: str) -> PreprocessManager:
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token")
    monkeypatch.setenv("MINERU_REQUEST_MAX_RETRIES", config.pop("retries", "3"))
    monkeypatch.setenv("MINERU_RETRY_BACKOFF_SECONDS", "0")
    return PreprocessManager(
        config={
            "Preprocess": {
                "parser_mode": "remote_first",
                "primary_parser": "mineru_remote",
                **config,
            }
        }
    )


def test_mineru_preflight_rejects_invalid_endpoint_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token")
    monkeypatch.setenv("MINERU_BASE_URL", "http://mineru.invalid/api")
    manager = PreprocessManager(config={})
    monkeypatch.setattr(
        requests,
        "request",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network must not be used")),
    )

    with pytest.raises(ValueError, match="absolute HTTPS"):
        manager.preflight_mineru()


@pytest.mark.parametrize("status_code", [401, 403])
def test_mineru_authorization_failure_opens_job_circuit_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    manager = _remote_manager(monkeypatch)
    calls: list[str] = []

    def fake_request(**kwargs):
        calls.append(str(kwargs["url"]))
        return _Response(status_code)

    monkeypatch.setattr(requests, "request", fake_request)
    with pytest.raises(ProviderCircuitOpen):
        manager._request_json("get", f"{manager.mineru_base_url}/status")

    assert len(calls) == 1
    assert manager.mineru_circuit_breaker.snapshot.open is True
    assert manager.mineru_circuit_breaker.snapshot.status_code == status_code


def test_mineru_circuit_is_shared_across_per_paper_managers(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = ProviderCircuitBreaker("mineru")
    breaker.open(reason="authorization_rejected", status_code=401)
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token")
    first = PreprocessManager(config={}, mineru_circuit_breaker=breaker)
    second = PreprocessManager(config={}, mineru_circuit_breaker=breaker)

    with pytest.raises(ProviderCircuitOpen):
        first.preflight_mineru()
    with pytest.raises(ProviderCircuitOpen):
        second.preflight_mineru()

    monkeypatch.setattr(
        requests,
        "request",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("open circuit must block network")),
    )
    with pytest.raises(ProviderCircuitOpen):
        second._request_json("get", f"{second.mineru_base_url}/status")


def test_mineru_transient_failure_still_uses_bounded_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _remote_manager(monkeypatch, retries="1")
    calls = 0

    def fake_request(**_kwargs):
        nonlocal calls
        calls += 1
        return _Response(500)

    monkeypatch.setattr(requests, "request", fake_request)
    with pytest.raises(requests.HTTPError):
        manager._request_json("get", f"{manager.mineru_base_url}/status")
    assert calls == 2
    assert manager.mineru_circuit_breaker.snapshot.open is False


def test_mineru_task_create_post_is_never_retried_after_an_ambiguous_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _remote_manager(monkeypatch, retries="5")
    calls = 0

    def ambiguous_post(**_kwargs):
        nonlocal calls
        calls += 1
        raise requests.Timeout("response lost after submission")

    monkeypatch.setattr(requests, "request", ambiguous_post)

    with pytest.raises(MineruSubmissionUncertainError):
        manager._request_json("post", f"{manager.mineru_base_url}/file-urls/batch", json={})

    assert calls == 1


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("mineru_poll_interval_seconds", "0"),
        ("mineru_poll_timeout_seconds", "3601"),
        ("mineru_request_max_retries", "6"),
        ("mineru_retry_backoff_seconds", "nan"),
        ("docling_timeout_seconds", "0"),
        ("ocr_timeout_seconds", "601"),
    ),
)
def test_preprocess_manager_rejects_unbounded_mineru_and_local_worker_limits(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(MineruConfigurationError):
        PreprocessManager(
            config={"Preprocess": {field_name: value}},
            preprocess_environment_resolved=True,
        )


def test_mineru_remote_transport_records_bounded_redacted_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = PreprocessManager(
        config={
            "Preprocess": {
                "parser_mode": "remote_first",
                "primary_parser": "mineru_remote",
                "fallback_parser": "none",
                "mineru_api_token": "configured-token",
                "mineru_base_url": "https://mineru.example.test/api",
                "mineru_allowed_url_hosts": "storage.example.test",
                "mineru_request_max_retries": "2",
                "mineru_max_remote_tasks": "1",
                "mineru_max_remote_http_calls": "3",
                "mineru_max_remote_upload_bytes": "1024",
            }
        },
        preprocess_environment_resolved=True,
    )
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF fixture")
    request_methods: list[str] = []

    def fake_request(**kwargs):
        method = str(kwargs["method"])
        request_methods.append(method)
        if method == "POST":
            return _Response(
                200,
                {
                    "batch_id": "task-opaque-id",
                    "upload_urls": ["https://storage.example.test/upload"],
                },
            )
        return _Response(
            200,
            {
                "status": "success",
                "markdown_text": "# MinerU\n\ncomplete parsed document",
                "plain_text": "complete parsed document",
            },
        )

    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setattr(requests, "put", lambda *_args, **_kwargs: _Response(200))

    result = manager._extract_with_mineru_remote(
        pdf_path=str(pdf_path),
        baseline_page_diagnostics=[],
        baseline_page_blocks=[],
        baseline_page_index=[],
    )
    assert result is not None
    receipt = manager._finish_mineru_trace(
        status="remote_success",
        response_identity=manager._mineru_response_identity(result),
    )

    assert request_methods == ["POST", "GET"]
    assert receipt["network_calls"] == 3
    assert receipt["upload_bytes"] == pdf_path.stat().st_size
    assert receipt["task_id_hash"]
    assert "task-opaque-id" not in json.dumps(receipt, ensure_ascii=False)
    assert receipt["budget"]["tasks_used"] == 1
    assert receipt["budget"]["http_calls_used"] == 3


def test_mineru_result_asset_authorization_failure_opens_circuit_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_ALLOWED_URL_HOSTS", "cdn.example.test")
    manager = _remote_manager(monkeypatch)
    calls = 0

    class _Session:
        trust_env = True

        def get(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return _Response(403)

        def close(self) -> None:
            pass

    monkeypatch.setattr(requests, "Session", _Session)
    with pytest.raises(ProviderCircuitOpen):
        manager._request_binary("https://cdn.example.test/result.zip")
    assert calls == 1
    assert manager.mineru_circuit_breaker.snapshot.open is True


def test_docling_runs_through_bounded_json_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PreprocessManager(config={"Preprocess": {"docling_timeout_seconds": "2"}})

    def fake_run(command, **kwargs):
        assert Path(command[-2]).is_absolute()
        output = Path(command[-1])
        output.write_text(
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "markdown_text": "# 结果",
                        "plain_text": "结果",
                        "structured_payload": {},
                    },
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = manager._extract_with_docling(str(tmp_path / "paper.pdf"), [], [], [])

    assert result is not None
    assert result["extractor_used"] == "docling"
    assert result["plain_text"] == "结果"


def test_docling_timeout_fails_closed_to_next_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PreprocessManager(config={"Preprocess": {"docling_timeout_seconds": "1"}})

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert manager._extract_with_docling(str(tmp_path / "paper.pdf"), [], [], []) is None


def test_ocr_runs_through_bounded_json_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PreprocessManager(config={"Preprocess": {"ocr_timeout_seconds": "2"}})
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"fixture")

    class _Parent:
        name = str(pdf_path)

    class _Page:
        parent = _Parent()
        number = 2

    def fake_run(command, **kwargs):
        assert Path(command[-4]).is_absolute()
        Path(command[-1]).write_text(
            json.dumps({"ok": True, "text": "OCR 文本"}, ensure_ascii=True),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert manager._ocr_page(_Page()) == "OCR 文本"


def test_metadata_only_summary_is_marked_for_manual_review_by_current_schema() -> None:
    summary = normalize_ai_summary(
        {
            "paper_metadata": {
                "title": "Metadata Only Paper",
                "authors": ["Alice Example"],
                "year": None,
                "journal": None,
                "doi": None,
            }
        }
    )

    quality = summary["quality_audit"]
    assert quality["needs_manual_review"] is True
    assert "core_analysis.summary" in quality["missing_critical_fields"]
