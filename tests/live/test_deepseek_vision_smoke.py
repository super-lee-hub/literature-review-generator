"""Explicit live smoke for the configured DeepSeek Vision transport."""

from __future__ import annotations

import os
from pathlib import Path

import fitz
import pytest

import ai_interface
from runtime.provider_runtime import ProviderBudgetV1, ProviderRuntime, ProviderRuntimeLedger


_MODEL = "deepseek-v4-flash-vision-exp"
_API_BASE = "https://api.deepseek.com"


def _live_key() -> str:
    return str(
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("AUTO_GENERATE_DEEPSEEK_API_KEY")
        or ""
    ).strip()


def _live_gate_reason() -> str | None:
    if os.environ.get("AUTO_GENERATE_RUN_LIVE_API") != "1":
        return "LIVE_DEEPSEEK_VISION_SMOKE=NOT_RUN_LIVE_API_NOT_ENABLED"
    if not _live_key():
        return "LIVE_DEEPSEEK_VISION_SMOKE=NOT_RUN_NO_DEEPSEEK_KEY"
    return None


def _build_smoke_pdf_and_page_image(tmp_path: Path) -> Path:
    """Create a private one-page PDF containing a tiny table and framework."""

    pdf_path = tmp_path / "vision-smoke.pdf"
    image_path = tmp_path / "vision-smoke-page.png"
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((48, 48), "Vision transport smoke test", fontsize=18)
    page.insert_text((48, 78), "A = 12; B = 18", fontsize=11)
    page.draw_rect(fitz.Rect(48, 110, 270, 220), color=(0, 0, 0), width=1)
    page.draw_line((48, 145), (270, 145), color=(0, 0, 0), width=1)
    page.draw_line((120, 110), (120, 220), color=(0, 0, 0), width=1)
    page.draw_line((195, 110), (195, 220), color=(0, 0, 0), width=1)
    page.insert_text((60, 135), "Group", fontsize=10)
    page.insert_text((135, 135), "A", fontsize=10)
    page.insert_text((210, 135), "B", fontsize=10)
    page.insert_text((60, 170), "Control", fontsize=10)
    page.insert_text((135, 170), "12", fontsize=10)
    page.insert_text((210, 170), "18", fontsize=10)
    page.insert_text((60, 205), "Treatment", fontsize=10)
    page.insert_text((135, 205), "15", fontsize=10)
    page.insert_text((210, 205), "22", fontsize=10)
    page.insert_text((360, 135), "Input", fontsize=11)
    page.insert_text((360, 235), "Mechanism", fontsize=11)
    page.insert_text((360, 335), "Outcome", fontsize=11)
    page.draw_line((390, 145), (390, 225), color=(0, 0, 0), width=2)
    page.draw_line((390, 245), (390, 325), color=(0, 0, 0), width=2)
    page.draw_polyline([(384, 215), (390, 225), (396, 215)], color=(0, 0, 0), width=2)
    page.draw_polyline([(384, 315), (390, 325), (396, 315)], color=(0, 0, 0), width=2)
    document.save(pdf_path)
    document.close()
    source = fitz.open(pdf_path)
    try:
        pixmap = source[0].get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
        pixmap.save(image_path)
    finally:
        source.close()
    return image_path


@pytest.mark.live_api(provider="deepseek")
def test_deepseek_vision_live_smoke(tmp_path: Path) -> None:
    gate_reason = _live_gate_reason()
    if gate_reason is not None:
        print(gate_reason)
        pytest.skip(gate_reason)
    api_key = _live_key()

    image_path = _build_smoke_pdf_and_page_image(tmp_path)
    ledger = ProviderRuntimeLedger(tmp_path / "provider_receipts.jsonl")
    runtime = ProviderRuntime(
        budget=ProviderBudgetV1(max_calls=1, max_retries_per_call=0),
        ledger=ledger,
        job_id="live-deepseek-vision-smoke",
        attempt_id="live-smoke",
        stage_name="live_api_smoke",
        route="DeepSeek_Vision",
        node_id="deepseek-vision-smoke",
        call_id="deepseek-vision-smoke",
        endpoint_type="chat_completions",
    )
    result = ai_interface._call_ai_api_detailed(
        "Return JSON with one key named observed_text containing the visible image content.",
        {
            "api_key": api_key,
            "model": _MODEL,
            "api_base": _API_BASE,
            "proxy_mode": "direct",
            "provider_family": "deepseek",
            "endpoint_type": "chat_completions",
            "thinking": "disabled",
            "reasoning_effort": "high",
            "transport_retries": 0,
            "read_timeout_seconds": 90,
        },
        "You are a minimal multimodal transport smoke tester. Return valid JSON only.",
        max_tokens=6000,
        temperature=0.0,
        response_format="json",
        user_content=[
            {"type": "text", "text": "Inspect the attached image."},
            {"type": "local_image_path", "path": str(image_path), "visual_id": "smoke-image", "page_no": 1},
        ],
        retry_attempts=1,
        timeout_seconds=90,
        provider_runtime=runtime,
        provider_route="DeepSeek_Vision",
        max_single_image_bytes=1_000_000,
        max_request_image_bytes=2_000_000,
    )

    assert result.get("status") == "success", result.get("message") or result.get("error_kind")
    http_status = result.get("http_status")
    if http_status is not None:
        assert int(http_status) == 200
    transport = result.get("transport_metadata")
    assert isinstance(transport, dict)
    assert int(transport.get("images_actually_sent_count") or 0) > 0
    response_model = str(result.get("provider_response_model") or "").strip()
    if response_model:
        assert response_model == _MODEL
    usage_present = bool(result.get("provider_usage_present"))
    assert usage_present is True
    assert result.get("usage_status") == "reported"
    receipt = result.get("provider_receipt")
    assert isinstance(receipt, dict)
    assert receipt.get("status") == "success"
    assert int((receipt.get("metadata") or {}).get("images_actually_sent_count") or 0) > 0

    print("LIVE_DEEPSEEK_VISION_SMOKE=PASS")
    print(f"requested_model={_MODEL}")
    print(f"response_model={response_model or 'unavailable'}")
    print(f"image_parts={int(transport.get('images_actually_sent_count') or 0)}")
    print(f"http_status={http_status if http_status is not None else 'unavailable'}")
    print(f"usage_present={'true' if usage_present else 'false'}")
    print(f"receipt_status={receipt.get('status')}")
