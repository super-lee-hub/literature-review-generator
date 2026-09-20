"""Isolated OCR worker for one PDF page.

RapidOCR is the repository's portable OCR runtime.  Tesseract remains a
fallback for installations that provide it, but the production worker must
not require a separately installed Tesseract binary when RapidOCR is already
available in the locked environment.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Iterable

try:
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - compatibility with older PyMuPDF releases.
    import fitz  # type: ignore


def _extract_rapidocr_text(result: Any) -> str:
    """Normalize RapidOCR's version-dependent result shape to page text."""

    if result is None:
        return ""
    values: Iterable[Any]
    txts = getattr(result, "txts", None)
    if isinstance(txts, (list, tuple)):
        values = txts
    elif isinstance(result, (list, tuple)):
        values = result
    else:
        values = ()

    lines: list[str] = []
    for item in values:
        text: Any = ""
        if isinstance(item, dict):
            text = item.get("text") or item.get("txt") or ""
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            # The installed RapidOCR runtime returns [box, text, score].
            text = item[1]
        else:
            text = getattr(item, "text", "") or getattr(item, "txt", "")
        normalized = str(text or "").strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _rapidocr_text(page: Any) -> str:
    """Run the installed RapidOCR runtime against a rendered PDF page."""

    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        return ""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
    except ImportError:
        try:
            from rapidocr import RapidOCR  # type: ignore[import-not-found]
        except ImportError:
            return ""

    try:
        pixmap = page.get_pixmap(dpi=300, alpha=False)
    except Exception:
        return ""
    channels = int(getattr(pixmap, "n", 0) or 0)
    if channels not in {1, 3, 4}:
        return ""
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        int(pixmap.height), int(pixmap.width), channels
    )
    if channels == 4:
        image = image[:, :, :3]
    ocr_output: Any = RapidOCR()(image)
    result = ocr_output[0] if isinstance(ocr_output, tuple) and ocr_output else ocr_output
    return _extract_rapidocr_text(result)


def _tesseract_text(page: Any, languages: str) -> str:
    """Run PyMuPDF's Tesseract bridge when a Tesseract binary is present."""

    text_page = page.get_textpage_ocr(language=languages, dpi=300, full=True)
    text = page.get_text("text", textpage=text_page)
    return text if isinstance(text, str) else ""


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
        text = _rapidocr_text(page)
        engine = "rapidocr" if text else ""
        if not text:
            try:
                text = _tesseract_text(page, languages)
            except Exception:
                # A missing or unusable optional Tesseract fallback must not
                # hide the RapidOCR result or turn an OCR-poor page into a
                # fabricated success.
                text = ""
            if text:
                engine = "tesseract"
        payload = {"ok": True, "text": text, "engine": engine}
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    finally:
        if document is not None:
            document.close()
    output.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
