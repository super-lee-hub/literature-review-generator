import io
import json
import sys
from types import SimpleNamespace
import zipfile
from pathlib import Path

import fitz  # type: ignore
import pytest

from preprocess.service import PageDiagnostics, PreprocessManager


class _ListLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.records.append(("info", message))

    def warning(self, message: str) -> None:
        self.records.append(("warning", message))

    def error(self, message: str) -> None:
        self.records.append(("error", message))


def _make_text_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This is a test paper.\n" * 80)
    doc.save(path)
    doc.close()


def test_pymupdf4llm_respects_configured_ocr_mode(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_to_markdown(_path: str, **kwargs: object) -> str:
        calls.append(kwargs)
        return "# Extracted"

    monkeypatch.setitem(
        sys.modules,
        "pymupdf4llm",
        SimpleNamespace(to_markdown=fake_to_markdown),
    )

    off_manager = PreprocessManager(
        config={
            "Preprocess": {
                "enabled": "true",
                "ocr_mode": "off",
                "ocr_languages": "eng",
            }
        },
        logger=None,
    )
    always_manager = PreprocessManager(
        config={
            "Preprocess": {
                "enabled": "true",
                "ocr_mode": "always",
                "ocr_languages": "eng+chi_sim",
            }
        },
        logger=None,
    )

    assert off_manager._extract_with_pymupdf4llm("paper.pdf") == "# Extracted"
    assert always_manager._extract_with_pymupdf4llm("paper.pdf") == "# Extracted"
    assert calls == [
        {
            "use_ocr": False,
            "force_ocr": False,
            "ocr_language": "eng",
        },
        {
            "use_ocr": True,
            "force_ocr": True,
            "ocr_language": "eng+chi_sim",
        },
    ]


def test_preprocess_manager_generates_new_artifact_contract(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "sample.pdf"
    cache_dir = tmp_path / "cache"
    _make_text_pdf(pdf_path)
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)

    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(cache_dir),
            "ocr_mode": "off",
            "force_rebuild": "false",
            "extractor_profile": "fitz",  # 测试默认使用 fitz 提取器，避免 pymupdf4llm 导入
        },
    }
    manager = PreprocessManager(config=config, logger=None)
    result = manager.prepare_pdf(str(pdf_path))

    assert result is not None
    assert Path(result.markdown_path).name == "normalized.md"
    assert Path(result.plain_text_path).name == "plain_text.txt"
    assert Path(result.page_index_path).name == "page_index.json"
    assert Path(result.structured_json_path).name == "structured.json"
    assert Path(result.diagnostics_path).name == "diagnostics.json"
    assert Path(result.manifest_path).name == "prepare_manifest.json"
    assert Path(result.stage1_input_path).name == "stage1_input.md"
    assert Path(result.stage1_input_manifest_path).name == "stage1_input_manifest.json"
    assert Path(result.stage1_quality_report_path).name == "stage1_text_quality_report.json"
    assert Path(result.markdown_path).exists()
    assert Path(result.plain_text_path).exists()
    assert Path(result.page_index_path).exists()
    assert Path(result.structured_json_path).exists()
    assert Path(result.diagnostics_path).exists()
    assert Path(result.manifest_path).exists()
    assert Path(result.stage1_input_path).exists()
    assert Path(result.stage1_input_manifest_path).exists()
    assert Path(result.stage1_quality_report_path).exists()
    assert result.stage1_input_text
    assert result.selected_text_source in {"normalized_markdown", "plain_text", "markdown_from_plain_text"}
    assert result.stage1_quality_level in {"PASS", "WARN", "FALLBACK"}

    diagnostics = json.loads(Path(result.diagnostics_path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    stage1_manifest = json.loads(Path(result.stage1_input_manifest_path).read_text(encoding="utf-8"))
    quality_report = json.loads(Path(result.stage1_quality_report_path).read_text(encoding="utf-8"))
    chunks = json.loads((Path(result.cache_dir) / "chunks.json").read_text(encoding="utf-8"))
    assert diagnostics["extractor_used"] in {"fitz", "pymupdf4llm", "legacy_pdf_extractor"}
    assert diagnostics["mineru_token_present"] is False
    assert manifest["artifacts"]["normalized_md"] == result.markdown_path
    assert manifest["artifacts"]["stage1_input"] == result.stage1_input_path
    assert stage1_manifest["selected_text_source"] == result.selected_text_source
    assert stage1_manifest["artifacts"]["stage1_input"] == result.stage1_input_path
    assert quality_report["candidate_reports"]
    assert chunks
    assert {chunk["chunk_source"] for chunk in chunks} == {"selected_stage1_input"}


def test_preprocess_manager_defaults_to_local_even_with_ambient_mineru_token(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "sample.pdf"
    cache_dir = tmp_path / "cache"
    _make_text_pdf(pdf_path)
    monkeypatch.setenv("MINERU_API_TOKEN", "token")

    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(cache_dir),
            "ocr_mode": "off",
            "force_rebuild": "false",
            "extractor_profile": "fitz",  # 测试默认使用 fitz 提取器，避免 pymupdf4llm 导入
        },
    }
    manager = PreprocessManager(config=config, logger=None)
    result = manager.prepare_pdf(str(pdf_path))

    assert result is not None
    assert result.extractor_used in {"fitz", "pymupdf4llm", "legacy_pdf_extractor"}
    assert result.mineru_attempted is False
    assert result.mineru_token_present is True
    assert result.mineru_remote_requested is False
    assert result.mineru_remote_enabled is False


def test_preprocess_manager_records_hybrid_skip_reason_without_losing_token_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    cache_dir = tmp_path / "cache"
    _make_text_pdf(pdf_path)
    monkeypatch.setenv("MINERU_API_TOKEN", "token")

    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(cache_dir),
            "parser_mode": "hybrid",
            "primary_parser": "mineru_remote",
            "fallback_parser": "local",
            "ocr_mode": "off",
            "force_rebuild": "true",
            "extractor_profile": "fitz",
        },
    }
    manager = PreprocessManager(config=config, logger=None)
    monkeypatch.setattr(manager, "_should_try_remote_in_hybrid", lambda **_kwargs: False)
    result = manager.prepare_pdf(str(pdf_path))

    assert result is not None
    assert result.extractor_used in {"fitz", "pymupdf4llm", "legacy_pdf_extractor"}
    assert result.mineru_attempted is False
    assert result.mineru_succeeded is False
    assert result.mineru_token_present is True
    assert result.mineru_remote_requested is True
    assert result.mineru_remote_enabled is False

    diagnostics = json.loads(Path(result.diagnostics_path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert diagnostics["mineru_token_present"] is True
    assert diagnostics["mineru_remote_requested"] is True
    assert diagnostics["mineru_remote_enabled"] is False
    assert manifest["mineru_token_present"] is True
    assert manifest["mineru_remote_requested"] is True
    assert manifest["mineru_remote_enabled"] is False


def test_preprocess_manager_reuses_fresh_cache(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "sample.pdf"
    cache_dir = tmp_path / "cache"
    _make_text_pdf(pdf_path)

    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(cache_dir),
            "ocr_mode": "off",
            "force_rebuild": "false",
            "extractor_profile": "fitz",  # 测试默认使用 fitz 提取器，避免 pymupdf4llm 导入
        },
    }
    manager = PreprocessManager(config=config, logger=None)
    first = manager.prepare_pdf(str(pdf_path))
    assert first is not None
    for path in [
        first.stage1_input_path,
        first.stage1_input_manifest_path,
        first.stage1_quality_report_path,
    ]:
        Path(path).unlink()

    monkeypatch.setattr(
        manager,
        "_extract_preferred_content",
        lambda _path: (_ for _ in ()).throw(AssertionError("fresh cache should not re-run parser")),
    )
    second = manager.prepare_pdf(str(pdf_path))

    assert second is not None
    assert first.cache_dir == second.cache_dir
    assert first.manifest_path == second.manifest_path
    assert first.page_index_path == second.page_index_path
    assert Path(second.stage1_input_path).exists()
    assert Path(second.stage1_input_manifest_path).exists()
    assert Path(second.stage1_quality_report_path).exists()
    assert second.stage1_input_text
    chunks = json.loads((Path(second.cache_dir) / "chunks.json").read_text(encoding="utf-8"))
    assert chunks
    assert {chunk["chunk_source"] for chunk in chunks} == {"selected_stage1_input"}


def test_preprocess_manager_refreshes_stale_stage1_cache_with_completeness_gate(tmp_path: Path) -> None:
    manager = PreprocessManager(config={"Preprocess": {"enabled": "true"}}, logger=None)
    short_text = ("This paper introduces constructive choice processes in consumer behavior.\n" * 55).strip()
    artifact_paths = {
        "stage1_input_path": str(tmp_path / "stage1_input.md"),
        "stage1_input_manifest_path": str(tmp_path / "stage1_input_manifest.json"),
        "stage1_quality_report_path": str(tmp_path / "stage1_text_quality_report.json"),
        "chunks_path": str(tmp_path / "chunks.json"),
        "manifest_path": str(tmp_path / "prepare_manifest.json"),
        "diagnostics_path": str(tmp_path / "diagnostics.json"),
    }
    Path(artifact_paths["stage1_input_path"]).write_text(short_text, encoding="utf-8")
    Path(artifact_paths["stage1_input_manifest_path"]).write_text(
        json.dumps(
            {
                "selected_text_source": "normalized_markdown",
                "stage1_quality_level": "PASS",
                "stage1_quality_reasons": [],
                "selected_text_length": len(short_text),
                "page_count": 11,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    Path(artifact_paths["stage1_quality_report_path"]).write_text("{}", encoding="utf-8")
    Path(artifact_paths["chunks_path"]).write_text(
        json.dumps([{"chunk_source": "selected_stage1_input", "text": short_text}], ensure_ascii=False),
        encoding="utf-8",
    )

    selected_text, selected_source, quality_level, reasons, chunks = manager._load_or_rebuild_stage1_selection(
        markdown_text=short_text,
        plain_text=short_text,
        page_index=[{"page_number": index + 1, "text": "page text"} for index in range(11)],
        artifact_paths=artifact_paths,
        diagnostics={},
        manifest={},
    )

    assert selected_text == ""
    assert selected_source == ""
    assert quality_level == "REPROCESS"
    assert "incomplete_by_page_count" in reasons
    assert chunks == []
    refreshed_manifest = json.loads(Path(artifact_paths["stage1_input_manifest_path"]).read_text(encoding="utf-8"))
    assert refreshed_manifest["completeness_metrics"]["page_count"] == 11
    assert "incomplete_by_page_count" in refreshed_manifest["stage1_quality_reasons"]


def test_preprocess_manager_sanitizes_bytes_before_writing_json(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "sample.pdf"
    cache_dir = tmp_path / "cache"
    _make_text_pdf(pdf_path)

    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(cache_dir),
            "parser_mode": "remote_first",
            "primary_parser": "mineru_remote",
            "ocr_mode": "off",
            "force_rebuild": "true",
            "extractor_profile": "fitz",
        },
    }
    manager = PreprocessManager(config=config, logger=None)

    class FakePage:
        number = 0

        def get_text(self, mode: str, **_kwargs):
            if mode == "text":
                return "This is a plain text body."
            if mode == "dict":
                return {"image": b"\x00\x01", "nested": [{"raw": b"abc"}]}
            raise AssertionError(f"Unexpected mode: {mode}")

        def get_images(self, full: bool = True):
            return [("img",)] if full else []

    class FakeDoc:
        page_count = 1

        def load_page(self, page_number: int):
            assert page_number == 0
            return FakePage()

        def close(self) -> None:
            pass

    monkeypatch.setattr("preprocess.service.fitz.open", lambda _path: FakeDoc())

    result = manager.prepare_pdf(str(pdf_path))

    assert result is not None
    structured = json.loads(Path(result.structured_json_path).read_text(encoding="utf-8"))
    assert structured["pages"][0]["blocks"]["image"] == "<bytes:2>"
    assert structured["pages"][0]["blocks"]["nested"][0]["raw"] == "<bytes:3>"


def test_preprocess_manager_prefers_remote_mineru_when_available(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "sample.pdf"
    cache_dir = tmp_path / "cache"
    _make_text_pdf(pdf_path)
    monkeypatch.setenv("MINERU_API_TOKEN", "token")

    config = {
        "Paths": {"output_path": str(tmp_path)},
        "Preprocess": {
            "enabled": "true",
            "cache_dir": str(cache_dir),
            "parser_mode": "remote_first",
            "primary_parser": "mineru_remote",
            "ocr_mode": "off",
            "force_rebuild": "true",
        },
    }
    manager = PreprocessManager(config=config, logger=None)

    def _fake_remote(*_args, **_kwargs):
        return {
            "markdown_text": "# Parsed by MinerU\n\ncontent",
            "plain_text": "content",
            "page_index": [{"page_number": 1, "text": "content", "text_length": 7, "image_count": 0, "block_count": 0, "scanned_candidate": False, "used_ocr": False, "low_quality": False}],
            "page_diagnostics": [],
            "page_blocks": [],
            "structured_payload": {"source": "mineru"},
            "extractor_used": "mineru",
            "layout_fidelity": "layout_aware",
            "conversion_used": "native_pdf",
            "used_ocr": False,
        }

    monkeypatch.setattr(manager, "_extract_with_mineru_remote", _fake_remote)

    result = manager.prepare_pdf(str(pdf_path))

    assert result is not None
    assert result.extractor_used == "mineru"
    assert result.mineru_attempted is True
    assert result.mineru_succeeded is True

    diagnostics = json.loads(Path(result.diagnostics_path).read_text(encoding="utf-8"))
    assert diagnostics["mineru_attempted"] is True
    assert diagnostics["mineru_succeeded"] is True


def test_mineru_normalizer_downloads_full_zip_url(monkeypatch) -> None:
    manager = PreprocessManager(config={"Preprocess": {"enabled": "true"}}, logger=None)
    markdown_text = "# Complete MinerU result\n\n" + (
        "This complete extraction includes methods, results, discussion, conclusions, and references.\n" * 200
    )
    plain_text = markdown_text.replace("# Complete MinerU result\n\n", "")
    raw_zip = io.BytesIO()
    with zipfile.ZipFile(raw_zip, "w") as archive:
        archive.writestr("normalized.md", markdown_text)
        archive.writestr("plain_text.txt", plain_text)
        archive.writestr(
            "page_index.json",
            json.dumps([{"page_number": 1, "text": plain_text, "text_length": len(plain_text)}]),
        )

    monkeypatch.setattr(manager, "_request_binary", lambda _url: raw_zip.getvalue())

    normalized = manager._normalize_mineru_payload(
        payload={"data": {"extract_result": [{"state": "done", "full_zip_url": "https://cdn.example/result.zip"}]}},
        baseline_page_diagnostics=[],
        baseline_page_blocks=[],
        baseline_page_index=[{"page_number": 1, "text": "baseline"}],
    )

    assert normalized is not None
    assert normalized["markdown_text"] == markdown_text
    assert normalized["plain_text"] == plain_text
    assert len(normalized["plain_text"]) > 6000


def test_mineru_normalizer_does_not_treat_baseline_as_success_when_zip_download_fails(monkeypatch) -> None:
    manager = PreprocessManager(config={"Preprocess": {"enabled": "true"}}, logger=None)
    monkeypatch.setattr(
        manager,
        "_request_binary",
        lambda _url: (_ for _ in ()).throw(RuntimeError("cdn unavailable")),
    )

    normalized = manager._normalize_mineru_payload(
        payload={"data": {"extract_result": [{"state": "done", "full_zip_url": "https://cdn.example/result.zip"}]}},
        baseline_page_diagnostics=[],
        baseline_page_blocks=[],
        baseline_page_index=[{"page_number": 1, "text": "baseline watermark text"}],
    )

    assert normalized is None


def test_mineru_binary_download_bypasses_environment_proxy(monkeypatch) -> None:
    manager = PreprocessManager(config={"Preprocess": {"enabled": "true"}}, logger=None)
    manager.mineru_api_token = "token"
    manager.mineru_allowed_url_hosts = {"cdn.example"}
    sessions: list[object] = []
    observed_kwargs: list[dict] = []

    class FakeResponse:
        content = b"zip-bytes"

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        def __init__(self) -> None:
            self.trust_env = True
            self.closed = False
            sessions.append(self)

        def get(self, _url: str, **_kwargs):
            assert self.trust_env is False
            observed_kwargs.append(_kwargs)
            return FakeResponse()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("preprocess.service.requests.Session", FakeSession)

    assert manager._request_binary("https://cdn.example/result.zip") == b"zip-bytes"
    assert sessions and getattr(sessions[0], "closed") is True
    assert observed_kwargs[0]["headers"] == {}
    assert observed_kwargs[0]["allow_redirects"] is False


def test_mineru_json_request_rejects_cross_origin_urls(monkeypatch) -> None:
    manager = PreprocessManager(config={"Preprocess": {"enabled": "true"}}, logger=None)
    manager.mineru_base_url = "https://mineru.example/api/v4"
    manager.mineru_api_token = "token"

    def fail_request(*_args, **_kwargs):
        raise AssertionError("cross-origin request should not be sent")

    monkeypatch.setattr("preprocess.service.requests.request", fail_request)

    with pytest.raises(RuntimeError, match="configured MinerU service origin"):
        manager._request_json("get", "https://attacker.example/status")


def test_mineru_binary_request_rejects_untrusted_hosts(monkeypatch) -> None:
    manager = PreprocessManager(config={"Preprocess": {"enabled": "true"}}, logger=None)
    manager.mineru_base_url = "https://mineru.example/api/v4"

    def fail_session():
        raise AssertionError("untrusted binary request should not create a session")

    monkeypatch.setattr("preprocess.service.requests.Session", fail_session)

    with pytest.raises(RuntimeError, match="host is not trusted"):
        manager._request_binary("https://attacker.example/result.zip")


def test_mineru_upload_rejects_untrusted_presigned_url(monkeypatch, tmp_path: Path) -> None:
    manager = PreprocessManager(config={"Preprocess": {"enabled": "true"}}, logger=None)
    manager.mineru_base_url = "https://mineru.example/api/v4"
    manager.mineru_api_token = "token"
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF fake")

    monkeypatch.setattr(
        manager,
        "_request_json",
        lambda *_args, **_kwargs: {"batch_id": "batch-1", "upload_urls": ["https://attacker.example/upload"]},
    )

    def fail_put(*_args, **_kwargs):
        raise AssertionError("PDF upload should not be sent to an untrusted host")

    monkeypatch.setattr("preprocess.service.requests.put", fail_put)

    with pytest.raises(RuntimeError, match="host is not trusted"):
        manager._extract_with_mineru_remote(
            pdf_path=str(pdf_path),
            baseline_page_diagnostics=[],
            baseline_page_blocks=[],
            baseline_page_index=[],
        )


def test_preprocess_manager_skips_docling_when_local_pipeline_is_healthy(monkeypatch) -> None:
    manager = PreprocessManager(
        config={
            "Preprocess": {
                "enabled": "true",
                "ocr_mode": "off",
            },
        },
        logger=None,
    )

    healthy_diagnostics = [
        PageDiagnostics(
            page_number=1,
            text_length=600,
            image_count=0,
            scanned_candidate=False,
            used_ocr=False,
            low_quality=False,
        )
    ]
    local_result = {
        "markdown_text": "# Healthy\n\ncontent",
        "plain_text": "Healthy content",
        "page_index": [{"page_number": 1, "text": "Healthy content"}],
        "page_diagnostics": healthy_diagnostics,
        "page_blocks": [],
        "structured_payload": {},
        "extractor_used": "fitz",
        "layout_fidelity": "page_text",
        "conversion_used": "native_pdf",
        "used_ocr": False,
    }

    monkeypatch.setattr(manager, "_extract_with_existing_local_pipeline", lambda _pdf_path: local_result)
    monkeypatch.setattr(
        manager,
        "_extract_with_docling",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("docling should not run")),
    )

    result = manager._extract_with_local_fallbacks(
        pdf_path="sample.pdf",
        baseline_plain_text="Healthy content",
        baseline_page_diagnostics=healthy_diagnostics,
        baseline_page_blocks=[],
        baseline_page_index=[{"page_number": 1, "text": "Healthy content"}],
    )

    assert result == local_result


def test_preprocess_manager_uses_docling_when_local_pipeline_is_low_quality(monkeypatch) -> None:
    manager = PreprocessManager(
        config={
            "Preprocess": {
                "enabled": "true",
                "ocr_mode": "off",
            },
        },
        logger=None,
    )

    low_quality_diagnostics = [
        PageDiagnostics(
            page_number=1,
            text_length=30,
            image_count=1,
            scanned_candidate=True,
            used_ocr=True,
            low_quality=True,
        )
    ]
    local_result = {
        "markdown_text": "",
        "plain_text": "short",
        "page_index": [{"page_number": 1, "text": "short"}],
        "page_diagnostics": low_quality_diagnostics,
        "page_blocks": [],
        "structured_payload": {},
        "extractor_used": "fitz",
        "layout_fidelity": "page_text",
        "conversion_used": "native_pdf",
        "used_ocr": True,
    }
    docling_result = {
        "markdown_text": "# Docling\n\ncontent",
        "plain_text": "Docling content",
        "page_index": [{"page_number": 1, "text": "Docling content"}],
        "page_diagnostics": low_quality_diagnostics,
        "page_blocks": [],
        "structured_payload": {"source": "docling"},
        "extractor_used": "docling",
        "layout_fidelity": "layout_aware",
        "conversion_used": "native_pdf",
        "used_ocr": False,
    }

    monkeypatch.setattr(manager, "_extract_with_existing_local_pipeline", lambda _pdf_path: local_result)
    monkeypatch.setattr(manager, "_extract_with_docling", lambda *_args, **_kwargs: docling_result)

    result = manager._extract_with_local_fallbacks(
        pdf_path="sample.pdf",
        baseline_plain_text="short",
        baseline_page_diagnostics=low_quality_diagnostics,
        baseline_page_blocks=[],
        baseline_page_index=[{"page_number": 1, "text": "short"}],
    )

    assert result == docling_result


def test_preprocess_manager_force_docling_strategy_bypasses_local_pipeline(monkeypatch) -> None:
    manager = PreprocessManager(
        config={
            "Preprocess": {
                "enabled": "true",
                "ocr_mode": "off",
            },
        },
        logger=None,
    )
    manager.force_docling_strategy = True

    diagnostics = [
        PageDiagnostics(
            page_number=1,
            text_length=600,
            image_count=0,
            scanned_candidate=False,
            used_ocr=False,
            low_quality=False,
        )
    ]
    docling_result = {
        "markdown_text": "# Docling\n\ncontent",
        "plain_text": "Docling content",
        "page_index": [{"page_number": 1, "text": "Docling content"}],
        "page_diagnostics": diagnostics,
        "page_blocks": [],
        "structured_payload": {"source": "docling"},
        "extractor_used": "docling",
        "layout_fidelity": "layout_aware",
        "conversion_used": "native_pdf",
        "used_ocr": False,
    }

    monkeypatch.setattr(
        manager,
        "_extract_with_existing_local_pipeline",
        lambda _pdf_path: (_ for _ in ()).throw(AssertionError("local pipeline should not run")),
    )
    monkeypatch.setattr(manager, "_extract_with_docling", lambda *_args, **_kwargs: docling_result)

    result = manager._extract_with_local_fallbacks(
        pdf_path="sample.pdf",
        baseline_plain_text="Healthy content",
        baseline_page_diagnostics=diagnostics,
        baseline_page_blocks=[],
        baseline_page_index=[{"page_number": 1, "text": "Healthy content"}],
    )

    assert result == docling_result


def test_hybrid_skip_logs_baseline_quality_metrics(monkeypatch) -> None:
    logger = _ListLogger()
    manager = PreprocessManager(
        config={
            "Preprocess": {
                "enabled": "true",
                "parser_mode": "hybrid",
                "primary_parser": "mineru_remote",
                "ocr_mode": "off",
            },
        },
        logger=logger,
    )

    diagnostics = [
        PageDiagnostics(
            page_number=1,
            text_length=1600,
            image_count=0,
            scanned_candidate=False,
            used_ocr=False,
            low_quality=False,
        )
    ]
    local_result = {
        "markdown_text": "# Local\n\ncontent",
        "plain_text": "content",
        "page_index": [{"page_number": 1, "text": "content"}],
        "page_diagnostics": diagnostics,
        "page_blocks": [],
        "structured_payload": {},
        "extractor_used": "fitz",
        "layout_fidelity": "page_text",
        "conversion_used": "native_pdf",
        "used_ocr": False,
    }

    monkeypatch.setattr(
        manager,
        "_extract_local_page_data",
        lambda _pdf_path, allow_ocr: ("A" * 1600, diagnostics, []),
    )
    monkeypatch.setattr(
        manager,
        "_extract_with_local_fallbacks",
        lambda **_kwargs: local_result,
    )

    result = manager._extract_preferred_content("sample.pdf")

    assert result == local_result
    assert any(
        "text_length=1600" in message and "low_quality_pages=0/1" in message and "scanned_candidate_pages=0/1" in message
        for _level, message in logger.records
    )


def test_hybrid_tries_mineru_when_baseline_is_incomplete_by_page_count(monkeypatch) -> None:
    logger = _ListLogger()
    manager = PreprocessManager(
        config={
            "Preprocess": {
                "enabled": "true",
                "parser_mode": "hybrid",
                "primary_parser": "mineru_remote",
                "ocr_mode": "off",
            },
        },
        logger=logger,
    )
    manager.mineru_api_token = "token"

    diagnostics = [
        PageDiagnostics(
            page_number=index + 1,
            text_length=349,
            image_count=0,
            scanned_candidate=False,
            used_ocr=False,
            low_quality=False,
        )
        for index in range(11)
    ]
    baseline_text = "A local baseline paragraph about consumer choice.\n" * 80
    remote_result = {
        "markdown_text": "# MinerU\n\n" + ("complete remote text\n" * 400),
        "plain_text": "complete remote text\n" * 400,
        "page_index": [{"page_number": index + 1, "text": "remote page text"} for index in range(11)],
        "page_diagnostics": diagnostics,
        "page_blocks": [],
        "structured_payload": {"source": "mineru"},
        "extractor_used": "mineru",
        "layout_fidelity": "layout_aware",
        "conversion_used": "native_pdf",
        "used_ocr": False,
    }

    monkeypatch.setattr(
        manager,
        "_extract_local_page_data",
        lambda _pdf_path, allow_ocr: (baseline_text, diagnostics, []),
    )
    monkeypatch.setattr(manager, "_extract_with_mineru_remote", lambda **_kwargs: remote_result)
    monkeypatch.setattr(
        manager,
        "_extract_with_local_fallbacks",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("local fallback should not run before MinerU")),
    )

    result = manager._extract_preferred_content("sample.pdf")

    assert result is not None
    assert result is remote_result
    assert result["mineru_attempted"] is True
    assert result["mineru_succeeded"] is True
    assert result["mineru_remote_enabled"] is True
    assert any("local baseline looks incomplete" in message for _level, message in logger.records)
