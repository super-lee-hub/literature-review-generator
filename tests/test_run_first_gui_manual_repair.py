from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def gui_app_module(monkeypatch: pytest.MonkeyPatch):
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = types.SimpleNamespace(notify=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    sys.modules.pop("gui.app", None)
    module = importlib.import_module("gui.app")
    return importlib.reload(module)


def _proposal() -> dict[str, Any]:
    return {
        "block_id": "section-1:block-3",
        "expected_anchor_hash": "a" * 64,
        "replacement_text": "Reviewer supplied bounded correction text.",
        "source_evidence_ids": ["paper-b_unknown_author"],
        "citation_mapping": {
            "occurrence_id": "occurrence-7",
            "expected_ref_id": "R001",
            "expected_paper_id": "paper-a_unknown_author",
            "replacement_ref_id": "R002",
            "replacement_paper_id": "paper-b_unknown_author",
        },
    }


def _controller(gui_app_module, tmp_path: Path, *, actor: str = "reviewer", reason: str = "reviewed source mapping"):
    controller = object.__new__(gui_app_module.WorkspaceController)
    controller.config_path = str(tmp_path / "config.ini")
    controller.state = {
        "paths": {"output_path": str(tmp_path / "output")},
        "lifecycle": {
            "manual_repair_proposal_json": "",
            "manual_repair_proposal_file": "",
        },
    }
    controller.lifecycle_actor = actor
    controller.lifecycle_reason = reason
    controller.lifecycle_snapshot = {}
    controller.test_mode = True
    calls: list[tuple[str, dict[str, Any]]] = []
    notifications: list[dict[str, Any]] = []

    def control_plane_call(operation: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((operation, kwargs))
        if operation == "inspect":
            return {
                "artifacts": [{
                    "artifact_type": "repair_plan",
                    "status": "ready",
                    "artifact_id": "repair_plan:verified-report-plan",
                }],
            }
        if operation == "repair_apply":
            return {"status": "quarantined", "mutation_performed": True, **kwargs}
        raise AssertionError(f"unexpected control-plane operation: {operation}")

    controller._control_plane_call = control_plane_call
    controller._notify_control_plane_result = lambda result: notifications.append(dict(result))
    controller.refresh_lifecycle = lambda **_kwargs: None
    return controller, calls, notifications


@pytest.mark.parametrize("source", ["inline", "file"])
def test_manual_repair_form_passes_exact_proposal_and_approval_to_existing_gate(
    gui_app_module,
    tmp_path: Path,
    source: str,
) -> None:
    controller, calls, _notifications = _controller(
        gui_app_module,
        tmp_path,
        actor="  reviewer-4  ",
        reason="  checked the cited source  ",
    )
    proposal = _proposal()
    if source == "inline":
        controller.state["lifecycle"]["manual_repair_proposal_json"] = json.dumps(proposal)
    else:
        proposal_path = tmp_path / "manual-proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        # Relative paths use the same config-directory convention as ReviewIntent.
        controller.state["lifecycle"]["manual_repair_proposal_file"] = proposal_path.name

    result = controller.apply_repair_plan_from_gui()

    assert result["status"] == "quarantined"
    apply_calls = [kwargs for operation, kwargs in calls if operation == "repair_apply"]
    assert len(apply_calls) == 1
    assert apply_calls[0] == {
        "plan_id": "repair_plan:verified-report-plan",
        "manual_proposal": proposal,
        "actor": "reviewer-4",
        "reason": "checked the cited source",
    }


def test_absent_manual_proposal_keeps_existing_auto_safe_apply_call(gui_app_module, tmp_path: Path) -> None:
    controller, calls, _notifications = _controller(gui_app_module, tmp_path, actor="", reason="")

    result = controller.apply_repair_plan_from_gui()

    assert result["status"] == "quarantined"
    assert calls == [
        ("inspect", {}),
        ("repair_apply", {"plan_id": "repair_plan:verified-report-plan"}),
    ]


@pytest.mark.parametrize(
    ("inline", "file", "actor", "reason", "expected_reason"),
    [
        ('{"block_id":', "", "reviewer", "reason", "syntax error"),
        ('{"block_id":"x","block_id":"y"}', "", "reviewer", "reason", "repeats the field"),
        (json.dumps({**_proposal(), "invented_conclusion": "A wins"}), "", "reviewer", "reason", "unsupported fields"),
        (json.dumps({"block_id": "section-1:block-3"}), "", "reviewer", "reason", "missing required fields"),
        (json.dumps(_proposal()), "manual-proposal.json", "reviewer", "reason", "not both"),
        (json.dumps(_proposal()), "", "", "reason", "manual repair requires a reviewer and reason"),
        (json.dumps(_proposal()), "", "reviewer", "  ", "manual repair requires a reviewer and reason"),
    ],
)
def test_invalid_manual_repair_inputs_block_before_repair_apply(
    gui_app_module,
    tmp_path: Path,
    inline: str,
    file: str,
    actor: str,
    reason: str,
    expected_reason: str,
) -> None:
    controller, calls, notifications = _controller(
        gui_app_module,
        tmp_path,
        actor=actor,
        reason=reason,
    )
    controller.state["lifecycle"].update({
        "manual_repair_proposal_json": inline,
        "manual_repair_proposal_file": file,
    })

    result = controller.apply_repair_plan_from_gui()

    assert result["status"] == "blocked"
    assert result["mutation_performed"] is False
    assert expected_reason in result["reason"]
    assert notifications and notifications[-1]["status"] == "blocked"
    assert not any(operation == "repair_apply" for operation, _kwargs in calls)
    # All these invalidities are detectable before even reading the ready plan.
    assert not any(operation == "inspect" for operation, _kwargs in calls)


def test_lifecycle_form_exposes_proposal_json_and_file_controls(gui_app_module, monkeypatch) -> None:
    class Element:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.bound: tuple[Any, str] | None = None
            self.props_calls: list[str] = []
            self.events: list[tuple[str, Any]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def classes(self, *_args):
            return self

        def props(self, value: str):
            self.props_calls.append(value)
            return self

        def bind_value(self, target: Any, key: str):
            self.bound = (target, key)
            return self

        def bind_visibility_from(self, *_args, **_kwargs):
            return self

        def on(self, event: str, callback: Any):
            self.events.append((event, callback))
            return self

    class FakeUi:
        def __init__(self) -> None:
            self.textareas: list[Element] = []
            self.inputs: list[Element] = []
            self.buttons: list[tuple[str, Element]] = []

        def refreshable(self, callback):
            return callback

        def card(self):
            return Element()

        def row(self, *_args, **_kwargs):
            return Element()

        def grid(self, *_args, **_kwargs):
            return Element()

        def label(self, *_args, **_kwargs):
            return Element()

        def badge(self, *_args, **_kwargs):
            return Element()

        def button(self, label: str, **kwargs):
            element = Element(**kwargs)
            self.buttons.append((label, element))
            return element

        def input(self, label: str, **kwargs):
            element = Element(label=label, **kwargs)
            self.inputs.append(element)
            return element

        def textarea(self, label: str, **kwargs):
            element = Element(label=label, **kwargs)
            self.textareas.append(element)
            return element

    fake_ui = FakeUi()
    monkeypatch.setattr(gui_app_module, "ui", fake_ui)
    path_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(gui_app_module, "_render_path_field", lambda _controller, **kwargs: path_calls.append(kwargs))
    lifecycle = {"manual_repair_proposal_json": "", "manual_repair_proposal_file": ""}
    controller = types.SimpleNamespace(
        t=lambda value: value,
        state={"lifecycle": lifecycle},
        lifecycle_actor="",
        lifecycle_reason="",
        lifecycle_busy=False,
        lifecycle_snapshot={"workspace_path": "", "inspection": {}, "validation_status": {}, "canonical_states": []},
        test_mode=True,
        refresh_lifecycle=lambda **_kwargs: None,
        inspect_validation_status=lambda: None,
        execute_current_validation=lambda: None,
        resume_current_workspace=lambda: None,
        cancel_current_workspace=lambda: None,
        export_current_workspace=lambda: None,
        create_repair_plan_from_gui=lambda: None,
        apply_repair_plan_from_gui=lambda: None,
        adopt_current_outline=lambda: None,
        promote_repaired_artifacts_from_gui=lambda: None,
    )

    gui_app_module._render_lifecycle_card(controller)

    proposal_textarea = next(
        item for item in fake_ui.textareas
        if item.kwargs.get("label") == "Manual repair proposal JSON (optional)"
    )
    assert proposal_textarea.bound == (lifecycle, "manual_repair_proposal_json")
    assert any("data-testid=lifecycle-manual-repair-proposal-json" in value for value in proposal_textarea.props_calls)
    assert path_calls == [{
        "label": "Manual repair proposal JSON file (optional)",
        "section": "lifecycle",
        "key": "manual_repair_proposal_file",
        "pick": "file",
        "title": "Select manual repair proposal JSON file",
        "filetypes": [("JSON Files", "*.json"), ("All Files", "*.*")],
        "test_id": "lifecycle-manual-repair-proposal-file",
    }]
    assert any(label == "Apply verified repair plan" for label, _element in fake_ui.buttons)
    assert [item.kwargs.get("label") for item in fake_ui.inputs] == [
        "Reviewer / lifecycle actor",
        "Reason for this lifecycle action",
    ]
