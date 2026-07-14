from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import cast

import pytest
import requests

from config_loader import ConfigDict
import main
from preprocess.provider_circuit import ProviderCircuitBreaker, ProviderCircuitOpen
from preprocess.service import PreprocessManager


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


def test_metadata_only_quality_failure_does_not_reprocess_with_docling_or_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = main.LiteratureReviewGenerator(project_name="demo", pdf_folder=str(tmp_path))
    generator.logger = cast(main.CustomLogger, _SilentLogger())
    generator.config = ConfigDict(
        {
            "Primary_Reader_API": {
                "api_key": "primary",
                "model": "m1",
                "api_base": "https://example.test/v1",
            },
            "Backup_Reader_API": {
                "api_key": "",
                "model": "",
                "api_base": "https://example.test/v1",
            },
        }
    )

    pdf_path = tmp_path / "metadata-only.pdf"
    pdf_path.write_bytes(b"synthetic pdf")
    manager = PreprocessManager(config={})
    prepare_calls: list[str] = []
    docling_calls: list[str] = []
    ocr_calls: list[object] = []

    def fake_docling(_self, path, *_args):
        docling_calls.append(str(path))
        return {
            "markdown_text": "# Extracted paper",
            "plain_text": "Extracted paper body. " * 80,
            "structured_payload": {},
            "extractor_used": "docling",
        }

    def fake_ocr(_self, page):
        ocr_calls.append(page)
        return "OCR fallback text. " * 80

    def prepare_stage1_input(path, preprocess_strategy="hybrid"):
        prepare_calls.append(preprocess_strategy)
        if preprocess_strategy == "hybrid":
            extracted = manager._extract_with_docling(path, [], [], [])
            assert extracted is not None
            return extracted["plain_text"], {
                "analysis_input_kind": "text",
                "extractor_used": "docling",
            }
        return manager._ocr_page(object()), {
            "analysis_input_kind": "text",
            "extractor_used": "ocr",
        }

    ai_summary = {
        "paper_metadata": {
            "title": "Metadata Only Paper",
            "authors": ["Alice Example"],
            "year": None,
            "journal": None,
            "doi": None,
        }
    }

    monkeypatch.setattr(PreprocessManager, "_extract_with_docling", fake_docling)
    monkeypatch.setattr(PreprocessManager, "_ocr_page", fake_ocr)
    monkeypatch.setattr(generator, "_stage1_preprocess_strategies", lambda: ["hybrid", "ocr"])
    monkeypatch.setattr(generator, "_prepare_stage1_input", prepare_stage1_input)
    monkeypatch.setattr(
        generator,
        "_build_stage1_model_input",
        lambda **_kwargs: {"prompt_text": "analyze", "user_message_content": None},
    )
    monkeypatch.setattr(
        generator,
        "_call_stage1_reader_with_scheduler",
        lambda *_args, **_kwargs: {"content": ai_summary, "engine_type": "primary"},
    )
    monkeypatch.setattr(
        main,
        "validate_summary_quality",
        lambda _result: (False, "year metadata missing; journal metadata missing"),
    )
    monkeypatch.setattr(generator, "_persist_paper_artifact", lambda _result: True)

    result = generator.process_paper(
        {
            "title": "Metadata Only Paper",
            "authors": ["Alice Example"],
            "year": "unknown",
            "journal": "unknown",
            "doi": "",
            "pdf_path": str(pdf_path),
        },
        0,
        None,
        1,
    )

    assert result is not None
    assert result["status"] == "success"
    result_summary = result.get("ai_summary")
    assert isinstance(result_summary, dict)
    assert result_summary["quality_audit"]["needs_manual_review"] is True
    assert prepare_calls == ["hybrid"]
    assert docling_calls == [str(pdf_path)]
    assert ocr_calls == []
