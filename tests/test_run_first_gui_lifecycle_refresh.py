from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any, Self

import pytest


@pytest.fixture
def gui_app_module(monkeypatch: pytest.MonkeyPatch):
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = types.SimpleNamespace(notify=lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    sys.modules.pop("gui.app", None)
    module = importlib.import_module("gui.app")
    return importlib.reload(module)


class _Element:
    def __init__(self, ui: _FakeUi, **kwargs: Any) -> None:
        self.ui = ui
        self.kwargs = kwargs
        self.value = kwargs.get("value")
        self.binding: tuple[Any, str] | None = None
        self.disabled = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def classes(self, *_args: Any) -> Self:
        return self

    def props(self, *_args: Any) -> Self:
        return self

    def bind_value(self, target: Any, key: str) -> Self:
        self.binding = (target, key)
        return self

    def fill(self, value: str) -> None:
        self.value = value
        assert self.binding is not None
        target, key = self.binding
        setattr(target, key, value)

    def disable(self) -> None:
        self.disabled = True

    def enable(self) -> None:
        self.disabled = False


class _FakeUi:
    def __init__(self) -> None:
        self.inputs: list[_Element] = []
        self.textareas: list[_Element] = []
        self.buttons: list[tuple[str, _Element]] = []
        self.timers: list[tuple[float, Any]] = []
        self.status_panel_render_count = 0

    def refreshable(self, callback: Any) -> Any:
        def render() -> Any:
            self.status_panel_render_count += 1
            return callback()

        render.refresh = render
        return render

    def card(self) -> _Element:
        return _Element(self)

    def row(self, *_args: Any, **_kwargs: Any) -> _Element:
        return _Element(self)

    def grid(self, *_args: Any, **_kwargs: Any) -> _Element:
        return _Element(self)

    def column(self, *_args: Any, **_kwargs: Any) -> _Element:
        return _Element(self)

    def label(self, *_args: Any, **_kwargs: Any) -> _Element:
        return _Element(self)

    def badge(self, *_args: Any, **_kwargs: Any) -> _Element:
        return _Element(self)

    def input(self, label: str, **kwargs: Any) -> _Element:
        element = _Element(self, label=label, **kwargs)
        self.inputs.append(element)
        return element

    def textarea(self, label: str, **kwargs: Any) -> _Element:
        element = _Element(self, label=label, **kwargs)
        self.textareas.append(element)
        return element

    def button(self, label: str, **kwargs: Any) -> _Element:
        element = _Element(self, **kwargs)
        self.buttons.append((label, element))
        return element

    def timer(self, interval: float, callback: Any) -> None:
        self.timers.append((interval, callback))


def _controller(gui_app_module, tmp_path: Path):
    controller = object.__new__(gui_app_module.WorkspaceController)
    controller.t = lambda value: value
    controller.state = {
        "lifecycle": {
            "manual_repair_proposal_json": "",
            "manual_repair_proposal_file": "",
        }
    }
    controller.lifecycle_actor = ""
    controller.lifecycle_reason = ""
    controller.lifecycle_busy = False
    controller.lifecycle_snapshot = {}
    controller.test_mode = False
    controller.client = None
    controller.notifications: list[tuple[str, dict[str, Any]]] = []
    controller.control_calls: list[tuple[str, dict[str, Any]]] = []
    controller.notify = lambda message, **kwargs: controller.notifications.append(
        (str(message), dict(kwargs))
    )

    def control_plane_call(operation: str, **kwargs: Any) -> dict[str, Any]:
        controller.control_calls.append((operation, kwargs))
        if operation == "inspect":
            return {
                "status": {"job_status": "completed"},
                "workspace_path": str(tmp_path / "demo__job-1"),
                "artifacts": [{
                    "artifact_id": "outline-v3:final_outline",
                    "artifact_type": "final_outline",
                    "status": "ready",
                    "content_hash": "a" * 64,
                }],
                "provider_receipts": {"complete": True},
            }
        if operation == "validation_status":
            return {"status": "not_requested", "validation_artifact": None}
        if operation in {"adopt", "cancel"}:
            return {"status": "succeeded", "mutation_performed": True}
        return {"status": "available", "mutation_performed": False}

    controller._control_plane_call = control_plane_call
    return controller


def test_lifecycle_timer_keeps_actor_reason_form_stable_and_cancel_available(
    gui_app_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_ui = _FakeUi()
    monkeypatch.setattr(gui_app_module, "ui", fake_ui)
    monkeypatch.setattr(gui_app_module, "_render_path_field", lambda *_args, **_kwargs: None)
    controller = _controller(gui_app_module, tmp_path)
    async def click_actions() -> None:
        gui_app_module._render_lifecycle_card(controller)
        await asyncio.sleep(0.05)
        actor = next(
            element for element in fake_ui.inputs
            if element.kwargs.get("label") == "Reviewer / lifecycle actor"
        )
        reason = next(
            element for element in fake_ui.inputs
            if element.kwargs.get("label") == "Reason for this lifecycle action"
        )
        assert actor.binding == (controller, "lifecycle_actor")
        assert reason.binding == (controller, "lifecycle_reason")
        actor.fill("reviewer-1")
        reason.fill("approve the verified source comparison")
        input_count = len(fake_ui.inputs)
        button_count = len(fake_ui.buttons)
        initial_panel_renders = fake_ui.status_panel_render_count
        timer_interval, timer_callback = fake_ui.timers[0]
        assert timer_interval == 4.0

        timer_callback()
        await asyncio.sleep(0.05)

        assert controller.lifecycle_actor == "reviewer-1"
        assert controller.lifecycle_reason == "approve the verified source comparison"
        assert len(fake_ui.inputs) == input_count
        assert len(fake_ui.buttons) == button_count
        assert fake_ui.status_panel_render_count == initial_panel_renders + 1
        assert controller.notifications == []

        adopt_button = next(
            element for label, element in fake_ui.buttons
            if label == "Adopt verified outline"
        )
        await adopt_button.kwargs["on_click"]()
        assert (
            "adopt",
            {
                "artifact_id": "outline-v3:final_outline",
                "actor": "reviewer-1",
                "reason": "approve the verified source comparison",
                "expected_hash": "a" * 64,
            },
        ) in controller.control_calls

        controller.lifecycle_busy = True
        cancel_button = next(
            element for label, element in fake_ui.buttons
            if label == "Request cancellation"
        )
        assert cancel_button.disabled is False
        await cancel_button.kwargs["on_click"]()
        assert any(operation == "cancel" for operation, _kwargs in controller.control_calls)
        assert controller.lifecycle_busy is True

    asyncio.run(click_actions())
