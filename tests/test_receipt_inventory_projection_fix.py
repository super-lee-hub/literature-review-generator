from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DRAFT_ROOT = Path(__file__).resolve().parents[1]


def _load_staged_module(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _record(artifact_type: str, artifact_role: str, path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_type=artifact_type,
        artifact_role=artifact_role,
        path=str(path),
    )


def test_receipt_inventory_reads_ledgers_but_ignores_closure_and_snapshot_json(
    tmp_path: Path,
) -> None:
    control_plane = _load_staged_module(
        "receipt_inventory_fix_control_plane_test",
        DRAFT_ROOT / "runtime" / "control_plane.py",
    )
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    ledger_path = artifacts_dir / "stage1_provider_receipts.jsonl"
    ledger_path.write_text(
        json.dumps({"receipt_id": "reader-call-1", "status": "success"}) + "\n",
        encoding="utf-8",
    )
    closure_path = artifacts_dir / "provider_receipt_closure.json"
    closure_path.write_text(
        json.dumps(
            {"artifact_type": "provider_receipt_closure", "payload": {"complete": True}},
            indent=2,
        ),
        encoding="utf-8",
    )
    snapshot_path = artifacts_dir / "review_receipt_snapshot.json"
    snapshot_path.write_text(
        json.dumps({"artifact_type": "review_provider_receipt_snapshot", "receipt": {}}, indent=2),
        encoding="utf-8",
    )
    workspace = SimpleNamespace(
        paths=SimpleNamespace(artifacts_dir=str(artifacts_dir)),
        root_dir=str(tmp_path),
    )
    result = control_plane.ReviewControlPlane._read_provider_receipts(
        workspace,
        [
            _record("provider_receipt_ledger", "provider_receipts", ledger_path),
            _record("provider_receipt_closure", "provider_receipt_closure", closure_path),
            _record("review_provider_receipt_snapshot", "provider_receipt_snapshot", snapshot_path),
        ],
    )

    assert result["complete"] is True
    assert result["count"] == 1
    assert result["malformed"] == []
    assert result["conflicts"] == []
    assert result["entries"][0]["receipt_id"] == "reader-call-1"
    assert result["paths"] == [str(ledger_path.resolve())]


def test_receipt_inventory_still_reports_malformed_and_conflicting_ledger_rows(
    tmp_path: Path,
) -> None:
    control_plane = _load_staged_module(
        "receipt_inventory_fix_control_plane_diagnostics_test",
        DRAFT_ROOT / "runtime" / "control_plane.py",
    )
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    first_path = artifacts_dir / "stage1_provider_receipts_attempt1.jsonl"
    first_path.write_text(
        json.dumps({"receipt_id": "shared-call", "status": "success"}) + "\n{invalid-json\n",
        encoding="utf-8",
    )
    second_path = artifacts_dir / "stage1_provider_receipts_attempt2.jsonl"
    second_path.write_text(
        json.dumps({"receipt_id": "shared-call", "status": "failed"}) + "\n",
        encoding="utf-8",
    )
    workspace = SimpleNamespace(
        paths=SimpleNamespace(artifacts_dir=str(artifacts_dir)),
        root_dir=str(tmp_path),
    )
    result = control_plane.ReviewControlPlane._read_provider_receipts(
        workspace,
        [
            _record("provider_receipt_ledger", "provider_receipts", first_path),
            _record("provider_receipt_ledger", "provider_receipts", second_path),
        ],
    )

    assert result["complete"] is False
    assert len(result["malformed"]) == 1
    assert "invalid JSON" in result["malformed"][0]
    assert result["conflicts"] == ["shared-call"]
    assert result["entries"] == []


def _gui_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = types.SimpleNamespace(notify=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    return _load_staged_module(
        "receipt_inventory_fix_gui_test",
        DRAFT_ROOT / "gui" / "app.py",
    )


def _lifecycle_states(gui_app: Any, *, reasons: list[str], inventory_complete: bool) -> list[str]:
    inspection = {
        "job_id": "job-receipt-projection",
        "status": {
            "job_id": "job-receipt-projection",
            "job_status": "completed",
            "completion_reasons": reasons,
        },
        "artifacts": [],
        "integrity": [],
        "provider_receipts": {"complete": inventory_complete},
    }
    return gui_app.WorkspaceController._canonical_lifecycle_states(
        inspection,
        {"status": "not_requested", "validation_artifact": None},
    )


def test_gui_does_not_project_historical_inventory_damage_as_current_closure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui_app = _gui_app(monkeypatch)
    states = _lifecycle_states(gui_app, reasons=[], inventory_complete=False)
    assert "receipt_incomplete" not in states


def test_gui_projects_canonical_closure_failure_even_if_inventory_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui_app = _gui_app(monkeypatch)
    states = _lifecycle_states(
        gui_app,
        reasons=["provider_receipts_incomplete"],
        inventory_complete=True,
    )
    assert "receipt_incomplete" in states
