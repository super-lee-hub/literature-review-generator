from __future__ import annotations

import json
from pathlib import Path

import reviewctl


def test_repair_promote_cli_passes_separate_validator_host_ack_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    acknowledgement = {
        "schema_version": "external-host-acknowledgement/v1",
        "acknowledged": True,
        "hosts": ["validator.example.test"],
        "route_fingerprint": "a" * 64,
    }
    acknowledgement_path = tmp_path / "validator-ack.json"
    acknowledgement_path.write_text(json.dumps(acknowledgement), encoding="utf-8")
    seen: list[dict] = []

    class FakeControl:
        def __init__(self, **_kwargs) -> None:
            pass

        def repair_promote(self, **kwargs):
            seen.append(kwargs)
            return {"status": "promoted"}

    monkeypatch.setattr(reviewctl, "ReviewControlPlane", FakeControl)
    exit_code = reviewctl.main([
        "repair-promote", "--job", "repair-job", "--transaction", "repair-tx:1",
        "--actor", "owner", "--reason", "approved recheck",
        "--validator-host-ack-file", str(acknowledgement_path),
    ])

    assert exit_code == 0
    assert len(seen) == 1
    assert seen[0]["validator_host_acknowledgement"] == acknowledgement
    assert json.loads(capsys.readouterr().out)["status"] == "promoted"


def test_repair_promote_cli_rejects_malformed_validator_host_ack_before_control_call(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    acknowledgement_path = tmp_path / "invalid-ack.json"
    acknowledgement_path.write_text("{invalid", encoding="utf-8")
    called = False

    class FakeControl:
        def __init__(self, **_kwargs) -> None:
            pass

        def repair_promote(self, **_kwargs):
            nonlocal called
            called = True
            return {"status": "promoted"}

    monkeypatch.setattr(reviewctl, "ReviewControlPlane", FakeControl)
    exit_code = reviewctl.main([
        "repair-promote", "--transaction", "repair-tx:1",
        "--actor", "owner", "--reason", "approved recheck",
        "--validator-host-ack-file", str(acknowledgement_path),
    ])

    assert exit_code == 2
    assert called is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "acknowledgement file" in payload["error"]
