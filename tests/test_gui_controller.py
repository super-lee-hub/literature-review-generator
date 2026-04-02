from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeElement:
    def __init__(self, *, deleted: bool = False, deleted_client: bool = False) -> None:
        self.is_deleted = deleted
        self._deleted_client = deleted_client
        self.text = ""
        self.value = ""
        self.disabled = False
        self.name = ""
        self.props_calls: list[tuple[tuple, dict]] = []
        self.class_calls: list[tuple[tuple, dict]] = []

    @property
    def client(self):
        if self._deleted_client:
            raise RuntimeError("The client this element belongs to has been deleted.")
        return object()

    def set_text(self, value: str) -> None:
        self.text = value

    def set_value(self, value):
        self.value = value

    def disable(self) -> None:
        self.disabled = True

    def enable(self) -> None:
        self.disabled = False

    def props(self, *args, **kwargs) -> None:
        self.props_calls.append((args, kwargs))

    def classes(self, *args, **kwargs) -> None:
        self.class_calls.append((args, kwargs))


class _FakeClient:
    def __init__(self, *, deleted: bool = False) -> None:
        self.deleted = deleted
        self.handlers = []

    def on_disconnect(self, handler) -> None:
        self.handlers.append(handler)

    def __enter__(self):
        if self.deleted:
            raise RuntimeError("The client this element belongs to has been deleted.")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.fixture()
def gui_app_module(monkeypatch):
    fake_ui = types.SimpleNamespace(notify=lambda *args, **kwargs: None)
    fake_nicegui = types.ModuleType("nicegui")
    setattr(fake_nicegui, "ui", fake_ui)
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    sys.modules.pop("gui.app", None)
    module = importlib.import_module("gui.app")
    return importlib.reload(module)


def test_refresh_logs_prunes_deleted_widgets(gui_app_module, monkeypatch) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    live_label = _FakeElement()
    stale_label = _FakeElement(deleted_client=True)
    live_view = _FakeElement()
    stale_view = _FakeElement(deleted_client=True)
    controller.bindings.log_path_labels = [stale_label, live_label]
    controller.bindings.log_views = [stale_view, live_view]

    monkeypatch.setattr(gui_app_module, "_latest_log_excerpt", lambda _language: ("D:/logs/demo.log", "latest lines"))

    controller.refresh_logs()

    assert controller.bindings.log_path_labels == [live_label]
    assert controller.bindings.log_views == [live_view]
    assert live_label.text == "D:/logs/demo.log"
    assert live_view.value == "latest lines"


def test_register_client_disconnect_prunes_stale_bindings_and_notify_is_safe(gui_app_module, monkeypatch) -> None:
    notifications = []
    monkeypatch.setattr(gui_app_module.ui, "notify", lambda *args, **kwargs: notifications.append((args, kwargs)))

    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    live_label = _FakeElement()
    stale_label = _FakeElement(deleted_client=True)
    controller.bindings.status_labels = [live_label, stale_label]

    client = _FakeClient()
    controller.register_client(client)
    assert len(client.handlers) == 1

    client.handlers[0](client)
    assert controller.bindings.status_labels == [live_label]

    deleted_client = _FakeClient(deleted=True)
    controller.client = deleted_client
    controller.notify("hello")

    assert notifications == []
    assert controller.client is None


def test_refresh_progress_updates_live_widgets_and_prunes_deleted_ones(gui_app_module) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))

    live_task = _FakeElement()
    stale_task = _FakeElement(deleted_client=True)
    live_item = _FakeElement()
    live_counts = _FakeElement()
    live_elapsed = _FakeElement()
    live_overall_bar = _FakeElement()
    live_stage_bar = _FakeElement()

    controller.bindings.progress_task_labels = [stale_task, live_task]
    controller.bindings.progress_item_labels = [live_item]
    controller.bindings.progress_counts_labels = [live_counts]
    controller.bindings.progress_elapsed_labels = [live_elapsed]
    controller.bindings.progress_overall_bars = [live_overall_bar]
    controller.bindings.progress_stage_bars = [live_stage_bar]
    controller.progress_snapshot = {
        "status": "running",
        "task_type": "文献分析",
        "stage": "analyze",
        "message": "已完成 1/2",
        "item_label": "Paper A",
        "success_count": 1,
        "failure_count": 0,
        "remaining_count": 1,
        "retry_round": 0,
        "retry_total_rounds": 0,
        "elapsed_seconds": 65,
        "total": 2,
        "current": 1,
        "indeterminate": False,
    }

    controller.update_progress_widgets()

    assert controller.bindings.progress_task_labels == [live_task]
    assert live_task.text == "文献分析"
    assert live_item.text == "Paper A"
    assert live_counts.text == "1 / 0 / 1"
    assert live_elapsed.text == "01:05"
    assert live_overall_bar.value == 0.5
    assert live_stage_bar.value == 0.5
