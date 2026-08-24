from __future__ import annotations

import ai_interface
from tests.test_current_stage1_generation import _canonical_summary


def test_backup_reader_receives_text_only_content_after_primary_failure(monkeypatch) -> None:
    observed: list[tuple[str, object]] = []

    def fake_detailed(prompt_text, primary, backup, *, engine_type="primary", user_content=None, **kwargs):
        observed.append((engine_type, user_content))
        if engine_type == "primary":
            return {"status": "failed", "error_kind": "quota_exhausted", "message": "quota"}
        return {"status": "success", "content": _canonical_summary(), "engine_type": "backup"}

    monkeypatch.setattr(ai_interface, "get_summary_from_ai_detailed", fake_detailed)
    result = ai_interface.get_summary_from_ai_with_fallback(
        "prompt",
        {
            "api_key": "primary",
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
        {
            "api_key": "backup",
            "model": "deepseek-v4-flash",
            "api_base": "https://api.deepseek.com",
            "provider_family": "deepseek",
        },
        user_content=[
            {"type": "text", "text": "label"},
            {"type": "local_image_path", "path": "missing.jpg"},
        ],
        return_detailed=True,
    )
    assert result["status"] == "success"
    assert result["fallback_reason"] == "quota"
    assert observed[1][0] == "backup"
