"""Isolated PyMuPDF/Tesseract OCR worker for one page."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import fitz  # type: ignore


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 4:
        return 2
    pdf_path, page_number, languages, output_path = args
    output = Path(output_path).resolve()
    document = None
    try:
        document = fitz.open(str(Path(pdf_path).resolve()))
        page = document.load_page(int(page_number))
        text_page = page.get_textpage_ocr(language=languages, dpi=300, full=True)
        text = page.get_text("text", textpage=text_page)
        payload = {"ok": True, "text": text if isinstance(text, str) else ""}
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    finally:
        if document is not None:
            document.close()
    output.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
