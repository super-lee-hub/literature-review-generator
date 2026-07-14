"""Windows-safe console and machine-readable JSON output helpers."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def write_ascii_json_line(payload: Any, *, stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    target.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    target.flush()
