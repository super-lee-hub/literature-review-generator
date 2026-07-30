from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import pph_stage1_reader_rebuild as reader
from services.artifact_registry import file_sha256


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _target(index: int = 1, zotero_key: str = "ZOTERO001") -> dict[str, Any]:
    return {
        "index": index,
        "source_order": index,
        "title": f"Paper {index}",
        "authors": ["Alice Smith"],
        "year": "2024",
        "doi": f"10.5555/test-{index}",
        "canonical_paper_key": f"paper-{index}",
        "zotero_parent_key": zotero_key,
        "pdf_sha256": f"{index:064x}",
        "source_pdf_sha256": f"{index:064x}",
        "stage1_input_sha256": f"{index + 100:064x}",
        "system_prompt_sha256": f"{index + 200:064x}",
    }


def _reader_record(
    work_dir: Path,
    *,
    target: dict[str, Any],
    prompt_sha256: str = "prompt-sha",
    receipt_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_path = work_dir / "stage1_reader_requests" / f"request_{target['index']:03d}.json"
    response_path = work_dir / "stage1_reader_responses" / f"response_{target['index']:03d}.json"
    system_prompt_path = work_dir / "stage1_reader_prompts" / "system.md"
    prompt_template_path = work_dir / "stage1_reader_prompts" / "template.md"
    source_pdf_path = work_dir / f"source_{target['index']:03d}.pdf"
    stage1_input_path = work_dir / f"stage1_input_{target['index']:03d}.md"
    system_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    system_prompt_path.write_text("system prompt", encoding="utf-8")
    prompt_template_path.write_text("prompt template", encoding="utf-8")
    source_pdf_path.write_bytes(bytes.fromhex(target["source_pdf_sha256"]))
    target["source_pdf_sha256"] = file_sha256(source_pdf_path)
    target["pdf_sha256"] = target["source_pdf_sha256"]
    stage1_input_path.write_text("stage 1 input", encoding="utf-8")
    target["stage1_input_sha256"] = file_sha256(stage1_input_path)
    request = {
        "request_timestamp": "2026-07-30T00:00:00Z",
        "canonical_paper_key": target["canonical_paper_key"],
        "zotero_parent_key": target["zotero_parent_key"],
        "source_pdf": str(source_pdf_path),
        "source_pdf_sha256": target["source_pdf_sha256"],
        "stage1_input_path": str(stage1_input_path),
        "stage1_input_sha256": target["stage1_input_sha256"],
        "prompt_sha256": prompt_sha256,
        "prompt_template_path": str(prompt_template_path),
        "prompt_template_sha256": file_sha256(prompt_template_path),
        "reader_route": "Primary_Reader_API",
        "provider": reader.PRIMARY_PROVIDER,
        "model": reader.PRIMARY_MODEL,
        "system_prompt_path": str(system_prompt_path),
        "system_prompt_sha256": file_sha256(system_prompt_path),
        "concurrency": 1,
        "fallback_allowed": False,
        "backup_reader_config_used": False,
    }
    _write_json(request_path, request)
    response = {
        "request_timestamp": "2026-07-30T00:00:00Z",
        "canonical_paper_key": target["canonical_paper_key"],
        "zotero_parent_key": target["zotero_parent_key"],
        "source_pdf_sha256": target["source_pdf_sha256"],
        "provider": reader.PRIMARY_PROVIDER,
        "model": reader.PRIMARY_MODEL,
        "fallback_allowed": False,
        "fallback_used": False,
        "request_path": str(request_path.resolve()),
        "request_sha256": file_sha256(request_path),
        "result": {
            "status": "success",
            "engine_type": "primary",
            "http_status": 200,
            "response_model": reader.PRIMARY_MODEL,
            "provider_response_id": "deepseek-response-id",
            "attempt_count": 1,
            "http_attempt_count": 1,
        },
        "content": {"ok": True},
    }
    _write_json(response_path, response)
    receipt = {
        "schema_version": "pph-stage1-reader-receipt-v1",
        "reader_route": "Primary_Reader_API",
        "provider": reader.PRIMARY_PROVIDER,
        "model": reader.PRIMARY_MODEL,
        "request_timestamp": "2026-07-30T00:00:00Z",
        "completed_at": "2026-07-30T00:00:01Z",
        "zotero_parent_key": target["zotero_parent_key"],
        "source_pdf_sha256": target["source_pdf_sha256"],
        "stage1_input_sha256": target["stage1_input_sha256"],
        "prompt_sha256": prompt_sha256,
        "system_prompt_sha256": target["system_prompt_sha256"],
        "request_path": str(request_path),
        "request_sha256": file_sha256(request_path),
        "response_path": str(response_path),
        "response_sha256": file_sha256(response_path),
        "http_status": 200,
        "provider_response_id": "deepseek-response-id",
        "response_model": reader.PRIMARY_MODEL,
        "attempt_count": 1,
        "http_attempt_count": 1,
        "concurrency": 1,
        "fallback_allowed": False,
        "fallback_used": False,
        "backup_reader_config_used": False,
    }
    receipt.update(receipt_overrides or {})
    return {
        "paper_info": {
            "canonical_paper_key": target["canonical_paper_key"],
            "zotero_parent_key": target["zotero_parent_key"],
            "source_pdf_fingerprint": target["source_pdf_sha256"],
        },
        "status": "success",
        "ai_summary": {
            "quality_audit": {
                "needs_manual_review": False,
                "missing_critical_fields": [],
                "completeness_score": 1.0,
            }
        },
        "stage1_reader_receipt": receipt,
    }


def _completed_event(record: dict[str, Any]) -> dict[str, Any]:
    receipt = record["stage1_reader_receipt"]
    return {
        "event": "request_completed",
        "zotero_parent_key": receipt["zotero_parent_key"],
        "status": "success",
        "provider": reader.PRIMARY_PROVIDER,
        "model": reader.PRIMARY_MODEL,
        "response_model": reader.PRIMARY_MODEL,
        "http_status": 200,
        "request_sha256": receipt["request_sha256"],
        "response_sha256": receipt["response_sha256"],
        "fallback_used": False,
    }


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("http_status", None),
        ("http_status", 500),
        ("response_model", ""),
        ("response_model", "deepseek-v4-flash"),
        ("provider_response_id", ""),
        ("attempt_count", 0),
        ("http_attempt_count", 0),
    ],
)
def test_valid_existing_reader_summary_rejects_incomplete_receipt_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    bad_value: Any,
) -> None:
    target = _target()
    prompt_sha256 = "prompt-sha"
    payload = _reader_record(
        tmp_path,
        target=target,
        prompt_sha256=prompt_sha256,
        receipt_overrides={field: bad_value},
    )
    summary_path = reader._reader_summary_path(tmp_path, target["index"])
    _write_json(summary_path, payload)
    monkeypatch.setattr(reader, "_validate_record_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr(reader, "_quality_gate", lambda *args, **kwargs: None)

    assert (
        reader._valid_existing_reader_summary(
            summary_path,
            target=target,
            prompt_sha256=prompt_sha256,
        )
        is None
    )


def test_valid_existing_reader_summary_rejects_system_prompt_sha_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    prompt_sha256 = "prompt-sha"
    payload = _reader_record(tmp_path, target=target, prompt_sha256=prompt_sha256)
    request_path = Path(payload["stage1_reader_receipt"]["request_path"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["system_prompt_sha256"] = "wrong-system-prompt-sha"
    _write_json(request_path, request)
    payload["stage1_reader_receipt"]["request_sha256"] = file_sha256(request_path)
    summary_path = reader._reader_summary_path(tmp_path, target["index"])
    _write_json(summary_path, payload)
    monkeypatch.setattr(reader, "_validate_record_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr(reader, "_quality_gate", lambda *args, **kwargs: None)

    assert (
        reader._valid_existing_reader_summary(
            summary_path,
            target=target,
            prompt_sha256=prompt_sha256,
        )
        is None
    )


def test_valid_existing_reader_summary_rejects_request_response_sha_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    prompt_sha256 = "prompt-sha"
    payload = _reader_record(tmp_path, target=target, prompt_sha256=prompt_sha256)
    Path(payload["stage1_reader_receipt"]["response_path"]).write_text(
        '{"tampered": true}',
        encoding="utf-8",
    )
    summary_path = reader._reader_summary_path(tmp_path, target["index"])
    _write_json(summary_path, payload)
    monkeypatch.setattr(reader, "_validate_record_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr(reader, "_quality_gate", lambda *args, **kwargs: None)

    assert (
        reader._valid_existing_reader_summary(
            summary_path,
            target=target,
            prompt_sha256=prompt_sha256,
        )
        is None
    )


def test_reader_manifest_counts_accepted_summaries_separately_from_transport_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reader, "EXPECTED_NEW_COUNT", 1)
    target = _target()
    targets_path = tmp_path / reader.READER_TARGETS_NAME
    _write_json(targets_path, {"targets": [target]})
    _write_json(tmp_path / "config.ini", {"config": True})
    _write_jsonl(
        tmp_path / reader.READER_LEDGER_NAME,
        [
            {
                "event": "request_completed",
                "zotero_parent_key": target["zotero_parent_key"],
                "status": "success",
                "http_attempt_count": 1,
                "fallback_used": False,
            }
        ],
    )

    manifest = reader._write_reader_manifest(
        work_dir=tmp_path,
        targets_path=targets_path,
        config_path=tmp_path / "config.ini",
        config={"Validator_API": {"model": "deepseek-v4-flash"}},
        primary_max_tokens=8000,
    )

    assert manifest["completed_request_count"] == 1
    assert manifest["successful_paper_count"] == 0
    assert manifest["remaining_paper_count"] == 1
    assert manifest["status"] == "incomplete"


def test_materialize_requires_accepted_reader_summaries_not_only_successful_request_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reusable_target = _target(1, "REUSE001")
    accepted_target = _target(2, "NEW001")
    rejected_target = _target(3, "NEW002")
    selected_rows = [reusable_target, accepted_target, rejected_target]
    crosswalk = [
        {"index": 1, "disposition": "reusable_exact"},
        {"index": 2, "disposition": "truly_new"},
        {"index": 3, "disposition": "truly_new"},
    ]
    monkeypatch.setattr(reader, "EXPECTED_CORPUS_COUNT", 3)
    monkeypatch.setattr(reader, "EXPECTED_REUSABLE_COUNT", 1)
    monkeypatch.setattr(reader, "EXPECTED_NEW_COUNT", 2)
    monkeypatch.setattr(
        reader,
        "_load_inputs",
        lambda **kwargs: (selected_rows, crosswalk, {}),
    )
    monkeypatch.setattr(reader, "_validate_record_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr(reader, "_quality_gate", lambda *args, **kwargs: None)

    reusable_record = _reader_record(tmp_path, target=reusable_target)
    _write_json(tmp_path / reader.REUSABLE_SUMMARIES_NAME, [reusable_record])
    accepted_record = _reader_record(tmp_path, target=accepted_target)
    rejected_record = _reader_record(tmp_path, target=rejected_target)
    _write_json(
        tmp_path / reader.READER_TARGETS_NAME,
        {"targets": [accepted_target, rejected_target]},
    )
    _write_json(
        reader._reader_summary_path(tmp_path, accepted_target["index"]),
        accepted_record,
    )
    _write_json(
        reader._reader_summary_path(tmp_path, rejected_target["index"]),
        rejected_record,
    )
    _write_jsonl(
        tmp_path / reader.READER_LEDGER_NAME,
        [_completed_event(accepted_record), _completed_event(rejected_record)],
    )
    selected_manifest = tmp_path / "selected.json"
    crosswalk_path = tmp_path / "crosswalk.jsonl"
    evidence_index_path = tmp_path / "evidence.json"
    _write_json(selected_manifest, {"selected": True})
    _write_jsonl(crosswalk_path, crosswalk)
    _write_json(evidence_index_path, {"evidence": True})

    with pytest.raises(reader.ReaderRebuildError, match="accepted summary"):
        reader.materialize(
            selected_manifest=selected_manifest,
            crosswalk_path=crosswalk_path,
            evidence_index_path=evidence_index_path,
            work_dir=tmp_path,
        )
