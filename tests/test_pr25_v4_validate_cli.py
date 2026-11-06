from __future__ import annotations

import json
from pathlib import Path

import reviewctl


def test_validate_cli_passes_separate_validator_acknowledgement(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    acknowledgement = {
        "schema_version": "external-host-acknowledgement/v1",
        "acknowledged": True,
        "hosts": ["validator.example.test"],
        "route_fingerprint": "a" * 64,
    }
    path = tmp_path / "validator-ack.json"
    path.write_text(json.dumps(acknowledgement), encoding="utf-8")
    seen: list[dict] = []

    class FakeControl:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, **kwargs):
            seen.append(kwargs)
            return {"status": "valid"}

    monkeypatch.setattr(reviewctl, "ReviewControlPlane", FakeControl)
    exit_code = reviewctl.main([
        "validate", "--job", "validation-job",
        "--validator-host-ack-file", str(path),
    ])

    assert exit_code == 0
    assert seen == [{
        "job_id": "validation-job",
        "workspace": None,
        "validator_host_acknowledgement": acknowledgement,
    }]
    assert json.loads(capsys.readouterr().out)["status"] == "valid"


def test_validate_cli_rejects_bad_acknowledgement_before_execution(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "invalid-ack.json"
    path.write_text("{invalid", encoding="utf-8")
    called = False

    class FakeControl:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, **_kwargs):
            nonlocal called
            called = True
            return {"status": "valid"}

    monkeypatch.setattr(reviewctl, "ReviewControlPlane", FakeControl)
    exit_code = reviewctl.main([
        "validate", "--validator-host-ack-file", str(path),
    ])

    assert exit_code == 2
    assert called is False
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_validate_cli_reports_blocked_as_failure(monkeypatch, capsys) -> None:
    class FakeControl:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, **_kwargs):
            return {"status": "blocked", "reason": "aggregate budget missing"}

    monkeypatch.setattr(reviewctl, "ReviewControlPlane", FakeControl)
    assert reviewctl.main(["validate", "--job", "validation-job"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
