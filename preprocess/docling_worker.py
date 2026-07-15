"""Isolated Docling conversion worker invoked as a subprocess."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def convert(pdf_path: str) -> dict:
    from docling.document_converter import DocumentConverter  # type: ignore

    converted = DocumentConverter().convert(pdf_path)
    document = getattr(converted, "document", None)
    if document is None:
        raise RuntimeError("Docling returned no document")
    markdown = str(document.export_to_markdown() or "") if hasattr(document, "export_to_markdown") else ""
    plain_text = str(document.export_to_text() or "") if hasattr(document, "export_to_text") else ""
    structured = document.export_to_dict() if hasattr(document, "export_to_dict") else {}
    return {
        "markdown_text": markdown,
        "plain_text": plain_text or markdown,
        "structured_payload": structured if isinstance(structured, dict) else {},
    }


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 2:
        return 2
    output = Path(args[1]).resolve()
    try:
        payload = {"ok": True, "result": convert(str(Path(args[0]).resolve()))}
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    output.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
