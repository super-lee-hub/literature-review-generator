import json
from pathlib import Path

import fitz  # type: ignore

from preprocess.service import PreprocessManager


def _make_text_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This is a test paper.\n" * 80)
    doc.save(path)
    doc.close()


def test_preprocess_manager_generates_new_artifact_contract(tmp_path: Path) -> None:
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
    assert Path(result.markdown_path).exists()
    assert Path(result.plain_text_path).exists()
    assert Path(result.page_index_path).exists()
    assert Path(result.structured_json_path).exists()
    assert Path(result.diagnostics_path).exists()
    assert Path(result.manifest_path).exists()

    diagnostics = json.loads(Path(result.diagnostics_path).read_text(encoding="utf-8"))
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert diagnostics["extractor_used"] in {"fitz", "pymupdf4llm", "legacy_pdf_extractor"}
    assert diagnostics["mineru_token_present"] is False
    assert manifest["artifacts"]["normalized_md"] == result.markdown_path


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
        },
    }
    manager = PreprocessManager(config=config, logger=None)
    result = manager.prepare_pdf(str(pdf_path))

    assert result is not None
    assert result.extractor_used in {"fitz", "pymupdf4llm", "legacy_pdf_extractor"}
    assert result.mineru_attempted is False


def test_preprocess_manager_reuses_fresh_cache(tmp_path: Path) -> None:
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
        },
    }
    manager = PreprocessManager(config=config, logger=None)
    first = manager.prepare_pdf(str(pdf_path))
    second = manager.prepare_pdf(str(pdf_path))

    assert first is not None
    assert second is not None
    assert first.cache_dir == second.cache_dir
    assert first.manifest_path == second.manifest_path
    assert first.page_index_path == second.page_index_path


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
