from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import reviewctl


def _disable_budget_start(monkeypatch: pytest.MonkeyPatch) -> None:
    import runtime.provider_runtime

    monkeypatch.setattr(
        runtime.provider_runtime,
        "provider_budget_controller_from_environment",
        lambda: None,
    )


def test_repair_apply_parser_accepts_optional_manual_proposal_inputs() -> None:
    args = reviewctl.build_parser().parse_args(
        [
            "repair-apply",
            "--plan",
            "repair-plan:current",
            "--manual-proposal-file",
            "proposal.json",
            "--actor",
            "reviewer",
            "--reason",
            "approved source-bound correction",
        ]
    )

    assert args.command == "repair-apply"
    assert args.plan == "repair-plan:current"
    assert args.manual_proposal_file == "proposal.json"
    assert args.actor == "reviewer"
    assert args.reason == "approved source-bound correction"


def test_repair_apply_passes_manual_proposal_unchanged_to_control_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _disable_budget_start(monkeypatch)
    proposal: dict[str, Any] = {
        "block_id": "block:claim-1",
        "expected_anchor_hash": "a" * 64,
        "replacement_text": "The source-bounded corrected claim [[cite_ref:R002]].",
        "source_evidence_ids": ["study-b"],
        "citation_mapping": {
            "occurrence_id": "occurrence-1",
            "expected_ref_id": "R001",
            "expected_paper_id": "study-a",
            "replacement_ref_id": "R002",
            "replacement_paper_id": "study-b",
        },
    }
    proposal_path = tmp_path / "approved-proposal.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    calls: list[dict[str, Any]] = []

    class ControlPlaneDouble:
        def __init__(self, *, repo_root: str | None = None) -> None:
            del repo_root

        def repair_apply(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"status": "available", "mutation_performed": False}

    monkeypatch.setattr(reviewctl, "ReviewControlPlane", ControlPlaneDouble)

    exit_code = reviewctl.main(
        [
            "repair-apply",
            "--workspace",
            "D:/work/demo__job-1",
            "--plan",
            "repair-plan:current",
            "--manual-proposal-file",
            str(proposal_path),
            "--actor",
            "reviewer-1",
            "--reason",
            "approved correction after source review",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0] == {
        "job_id": None,
        "workspace": "D:/work/demo__job-1",
        "plan_id": "repair-plan:current",
        "manual_proposal": proposal,
        "actor": "reviewer-1",
        "reason": "approved correction after source review",
    }
    assert json.loads(capsys.readouterr().out)["status"] == "available"


def test_repair_apply_without_manual_flags_preserves_existing_plan_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _disable_budget_start(monkeypatch)
    calls: list[dict[str, Any]] = []

    class ControlPlaneDouble:
        def __init__(self, *, repo_root: str | None = None) -> None:
            del repo_root

        def repair_apply(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"status": "available", "mutation_performed": False}

    monkeypatch.setattr(reviewctl, "ReviewControlPlane", ControlPlaneDouble)

    assert reviewctl.main(["repair-apply", "--plan", "repair-plan:auto"]) == 0

    assert len(calls) == 1
    assert calls[0]["manual_proposal"] is None
    assert calls[0]["actor"] == ""
    assert calls[0]["reason"] == ""
    assert json.loads(capsys.readouterr().out)["status"] == "available"


@pytest.mark.parametrize(
    ("raw_json", "expected_error"),
    [
        ("{", "manual repair proposal JSON is invalid"),
        ("[]", "manual repair proposal must be a JSON object"),
        ('{"block_id":"first","block_id":"second"}', "repeats JSON field"),
        ('{"nested":{"key":1,"key":2}}', "repeats JSON field"),
        ('{"amount":NaN}', "invalid JSON constant"),
    ],
)
def test_repair_apply_rejects_invalid_manual_proposal_before_control_plane(
    raw_json: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import runtime.provider_runtime

    proposal_path = tmp_path / "invalid-proposal.json"
    proposal_path.write_text(raw_json, encoding="utf-8")
    monkeypatch.setattr(
        runtime.provider_runtime,
        "provider_budget_controller_from_environment",
        lambda: pytest.fail("invalid repair JSON must be rejected before runtime setup"),
    )
    monkeypatch.setattr(
        reviewctl,
        "ReviewControlPlane",
        lambda **_kwargs: pytest.fail("invalid repair JSON must not reach the control plane"),
    )

    exit_code = reviewctl.main(
        [
            "repair-apply",
            "--plan",
            "repair-plan:current",
            "--manual-proposal-file",
            str(proposal_path),
            "--actor",
            "reviewer-1",
            "--reason",
            "approved after source review",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["read_only"] is True
    assert expected_error in payload["error"]


@pytest.mark.parametrize(
    ("extra_args", "expected_error"),
    [
        (("--manual-proposal-file", ""), "non-empty path"),
        (("--manual-proposal-file", "proposal.json", "--actor", "reviewer"), "both --actor and --reason"),
        (("--manual-proposal-file", "proposal.json", "--reason", "approved"), "both --actor and --reason"),
        (("--actor", "reviewer"), "require --manual-proposal-file"),
        (("--reason", "approved"), "require --manual-proposal-file"),
    ],
)
def test_repair_apply_rejects_incomplete_manual_approval_before_control_plane(
    extra_args: tuple[str, ...],
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import runtime.provider_runtime

    monkeypatch.setattr(
        runtime.provider_runtime,
        "provider_budget_controller_from_environment",
        lambda: pytest.fail("incomplete repair approval must be rejected before runtime setup"),
    )
    monkeypatch.setattr(
        reviewctl,
        "ReviewControlPlane",
        lambda **_kwargs: pytest.fail("incomplete repair approval must not reach the control plane"),
    )

    exit_code = reviewctl.main(
        ["repair-apply", "--plan", "repair-plan:current", *extra_args]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert expected_error in payload["error"]
