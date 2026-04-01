"""
Text decoding helpers shared by CLI and GUI workflows.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


TEXT_DECODE_CANDIDATES = ("utf-8", "utf-8-sig", "gb18030", "gbk")


def read_text_file_with_fallbacks(path: str | Path, logger: logging.Logger | None = None) -> str:
    target = Path(path)
    raw_bytes = target.read_bytes()

    for encoding in TEXT_DECODE_CANDIDATES:
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

    if logger:
        tried_encodings = ", ".join(TEXT_DECODE_CANDIDATES)
        logger.warning("文件无法按 %s 解码，改用 UTF-8 替换模式读取: %s", tried_encodings, target)

    return raw_bytes.decode("utf-8", errors="replace")


def load_json_file_with_fallbacks(path: str | Path, logger: logging.Logger | None = None) -> Any:
    return json.loads(read_text_file_with_fallbacks(path, logger=logger))
