from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import ai_interface
from test_current_stage1_generation import _canonical_summary, _service, _write_pdf


def test_current_stage1_default_reader_falls_back_from_primary_to_backup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    pdf_path = tmp_path / "fallback-paper.pdf"
    _write_pdf(pdf_path)
    engines: list[str] = []

    def fake_detailed(
        prompt_text: str,
        primary_api_config: Mapping[str, Any],
        backup_api_config: Mapping[str, Any],
        *,
        engine_type: str = "primary",
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        engines.append(engine_type)
        if engine_type == "primary":
            return {"status": "failed", "error_kind": "quota_exhausted", "message": "test quota"}
        return {"status": "success", "content": _canonical_summary()}

    monkeypatch.setattr(ai_interface, "get_summary_from_ai_detailed", fake_detailed)
    service, bundle = _service(tmp_path, pdf_path, reader=None)
    result = service.run(bundle)

    assert result.generated_count == 1
    assert engines[:2] == ["primary", "backup"]
