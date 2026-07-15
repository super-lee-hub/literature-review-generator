from __future__ import annotations

import io
import json

from services.console_io import write_ascii_json_line


def test_progress_json_is_ascii_safe_and_round_trips_unicode() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    write_ascii_json_line({"路径": "D:/中文/论文.pdf", "状态": "完成"}, stream=stream)
    stream.flush()
    stream.buffer.seek(0)
    raw = stream.buffer.read()

    assert raw.isascii()
    assert json.loads(raw.decode("ascii")) == {"路径": "D:/中文/论文.pdf", "状态": "完成"}
