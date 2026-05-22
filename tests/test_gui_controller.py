from __future__ import annotations

import asyncio
import configparser
import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest
from services.queue_service import QueueJobSpec


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


class _FakeQueueService:
    def __init__(self, job_ids: list[str]) -> None:
        self._job_ids = list(job_ids)

    def list_jobs(self):
        return [types.SimpleNamespace(job_id=job_id) for job_id in self._job_ids]


class _FakeBackgroundTask:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


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

    monkeypatch.setattr(gui_app_module, "_latest_log_excerpt", lambda _language, **_kwargs: ("D:/logs/demo.log", "latest lines"))

    controller.refresh_logs()

    assert controller.bindings.log_path_labels == [live_label]
    assert controller.bindings.log_views == [live_view]
    assert live_label.text == "D:/logs/demo.log"
    assert live_view.value == "latest lines"


def test_latest_log_excerpt_prefers_latest_job_workspace_log(gui_app_module, tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    workspace = output_root / "demo__job123"
    workspace_log = workspace / "logs" / "job.log"
    workspace_log.parent.mkdir(parents=True)
    workspace_log.write_text("workspace log line\n", encoding="utf-8")
    pointer_dir = output_root / "demo"
    pointer_dir.mkdir(parents=True)
    (pointer_dir / "_latest_job.json").write_text(
        json.dumps({"workspace_path": str(workspace)}),
        encoding="utf-8",
    )

    log_path, excerpt = gui_app_module._latest_log_excerpt(
        "zh-CN",
        output_root=output_root,
        project_name="demo",
        queue_service=None,
    )

    assert Path(log_path) == workspace_log
    assert excerpt == "workspace log line"


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


def test_move_queue_job_reorders_by_one_step(gui_app_module, monkeypatch) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    controller._queue_service = _FakeQueueService(["job-1", "job-2", "job-3"])

    captured_orders: list[list[str]] = []
    monkeypatch.setattr(controller, "reorder_jobs", lambda job_ids: captured_orders.append(list(job_ids)))

    controller.move_queue_job("job-2", -1)
    controller.move_queue_job("job-2", 1)
    controller.move_queue_job("job-1", -1)
    controller.move_queue_job("missing", 1)

    assert captured_orders == [
        ["job-2", "job-1", "job-3"],
        ["job-1", "job-3", "job-2"],
    ]


def test_validate_workflow_request_respects_selected_input_mode(gui_app_module, monkeypatch) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    notifications: list[str] = []
    monkeypatch.setattr(controller, "notify", lambda message, **_kwargs: notifications.append(message))

    controller.state["workflow"]["project_name"] = "demo"
    controller.state["workflow"]["input_mode"] = "pdf"
    controller.state["workflow"]["pdf_folder"] = ""
    assert controller.validate_workflow_request("analyze") is False
    assert notifications[-1] == "当前选择的是 PDF 文件夹模式，请先填写 PDF 文件夹。"

    controller.state["workflow"]["pdf_folder"] = "D:/papers"
    assert controller.validate_workflow_request("analyze") is True

    controller.state["workflow"]["input_mode"] = "zotero"
    controller.state["paths"]["zotero_report"] = ""
    controller.state["paths"]["library_path"] = ""
    assert controller.validate_workflow_request("analyze") is False
    assert notifications[-1] == "当前选择的是 Zotero 模式，请先填写 Zotero 报告路径。"

    controller.state["paths"]["zotero_report"] = "D:/zotero/report.md"
    assert controller.validate_workflow_request("analyze") is False
    assert notifications[-1] == "Zotero 模式还需要填写 Zotero 库路径。"

    controller.state["paths"]["library_path"] = "D:/Zotero"
    assert controller.validate_workflow_request("analyze") is True


def test_validate_workflow_request_only_blocks_pending_free_mode_when_free_mode_selected(gui_app_module, monkeypatch) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    notifications: list[str] = []
    monkeypatch.setattr(controller, "notify", lambda message, **_kwargs: notifications.append(message))

    controller.state["workflow"]["project_name"] = "demo"
    controller.state["workflow"]["input_mode"] = "pdf"
    controller.state["workflow"]["pdf_folder"] = "D:/papers"
    controller.free_mode_messages = [{"role": "user", "content": "test"}]
    controller.free_mode_profile_path = ""

    controller.state["workflow"]["work_mode"] = "normal"
    assert controller.validate_workflow_request("analyze") is True

    controller.state["workflow"]["work_mode"] = "free"
    assert controller.validate_workflow_request("analyze") is False
    assert notifications[-1] == "自由模式对话还没有应用到本次任务。请先应用当前规划，或清空对话后再运行。"


def test_build_queue_job_spec_can_use_explicit_input_mode(gui_app_module) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    controller.state["workflow"]["input_mode"] = "pdf"
    controller.state["workflow"]["work_mode"] = "free"
    controller.state["paths"]["library_path"] = "D:/ZoteroLibrary"

    spec = controller._build_queue_job_spec(
        "demo",
        "D:/papers",
        "D:/zotero/report.md",
        "analyze",
        input_mode="zotero",
        work_mode="normal",
    )

    assert spec.parameters["pdf_folder"] is None
    assert spec.parameters["zotero_report"] == "D:/zotero/report.md"
    assert spec.parameters["library_path"] == "D:/ZoteroLibrary"
    assert spec.parameters["source_mode"] == "zotero"
    assert spec.parameters["free_mode_profile"] is None
    assert spec.parameters["free_mode_idea"] is None


def test_stage1_reuse_defaults_to_analyze_only_actions(gui_app_module) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    controller.state["workflow"]["reuse_summary_files"] = "D:/reuse/a.json"

    analyze_spec = controller._build_queue_job_spec(
        "demo",
        "D:/papers",
        "",
        "analyze",
    )
    assert analyze_spec.parameters["reuse_stage1"] is True
    assert analyze_spec.parameters["reuse_summary_files"] == ["D:/reuse/a.json"]

    outline_spec = controller._build_queue_job_spec(
        "demo",
        "D:/papers",
        "",
        "outline",
    )
    assert outline_spec.parameters["reuse_stage1"] is False
    assert outline_spec.parameters["reuse_summary_files"] == []


def test_build_queue_job_spec_captures_immutable_source_snapshot(gui_app_module) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    controller.state["workflow"].update(
        {
            "input_mode": "pdf",
            "work_mode": "free",
            "summary_file": "D:/summaries/a.json",
            "summary_sources": "D:/summaries/b.json\nD:/summaries/c.json",
            "reuse_stage1": True,
            "reuse_summary_files": "D:/reuse/a.json",
            "free_mode_idea": "original idea",
            "section_number": "3",
        }
    )
    controller.state["paths"]["library_path"] = "D:/Library"
    controller.free_mode_profile_path = "D:/profiles/original.json"

    spec = controller._build_queue_job_spec(
        "original-project",
        "D:/papers/original",
        "",
        "generate_section",
    )

    controller.state["workflow"].update(
        {
            "summary_file": "D:/summaries/edited.json",
            "summary_sources": "D:/summaries/edited-source.json",
            "reuse_stage1": False,
            "reuse_summary_files": "D:/reuse/edited.json",
            "free_mode_idea": "edited idea",
            "section_number": "9",
        }
    )
    controller.free_mode_profile_path = "D:/profiles/edited.json"

    assert spec.source_snapshot == {
        "project_name": "original-project",
        "input_mode": "pdf",
        "work_mode": "free",
        "action": "generate_section",
        "pdf_folder": "D:/papers/original",
        "zotero_report": None,
        "library_path": None,
        "summary_file": "D:/summaries/a.json",
        "summary_sources": ["D:/summaries/b.json", "D:/summaries/c.json"],
        "reuse_stage1": False,
        "reuse_summary_files": [],
        "concept": None,
        "free_mode_profile": "D:/profiles/original.json",
        "free_mode_idea": None,
        "generate_section": 3,
    }
    assert spec.parameters["summary_sources"] == ["D:/summaries/b.json", "D:/summaries/c.json"]
    assert spec.parameters["reuse_summary_files"] == []


def test_schedule_queue_processor_keeps_single_background_task(gui_app_module, monkeypatch) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    controller._queue_runner = object()
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return _FakeBackgroundTask(done=False)

    monkeypatch.setattr(gui_app_module.asyncio, "create_task", fake_create_task)

    assert controller._schedule_queue_processor() is True
    assert controller._schedule_queue_processor() is False
    assert len(scheduled) == 1

    controller._queue_processor_task = None
    controller.queue_processor_running = True
    assert controller._schedule_queue_processor() is False
    assert len(scheduled) == 1


def test_run_workflow_enqueues_while_processor_running_without_block(gui_app_module, monkeypatch) -> None:
    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    controller.state["workflow"]["project_name"] = "demo"
    controller.state["workflow"]["input_mode"] = "pdf"
    controller.state["workflow"]["pdf_folder"] = "D:/papers"
    controller.queue_processor_running = True

    added_actions: list[str] = []
    notifications: list[str] = []
    schedule_calls: list[bool] = []

    monkeypatch.setattr(controller, "validate_workflow_request", lambda _action: True)
    monkeypatch.setattr(controller, "persist_config", lambda **_kwargs: None)
    monkeypatch.setattr(controller, "update_progress_widgets", lambda: None)
    monkeypatch.setattr(controller, "refresh_logs", lambda: None)
    monkeypatch.setattr(controller, "refresh_queue", lambda **_kwargs: None)
    monkeypatch.setattr(controller, "_queue_position", lambda _job_id: 2)
    monkeypatch.setattr(controller, "notify", lambda message, **_kwargs: notifications.append(message))

    def fake_add_job(project_name, pdf_folder, zotero_report, action):
        added_actions.append(action)
        return "job-queued"

    def fake_schedule():
        schedule_calls.append(True)
        return False

    monkeypatch.setattr(controller, "add_job_to_queue", fake_add_job)
    monkeypatch.setattr(controller, "_schedule_queue_processor", fake_schedule)

    asyncio.run(controller.run_workflow("analyze"))

    assert added_actions == ["analyze"]
    assert schedule_calls == [True]
    assert controller.workflow_running is False
    assert any("排队位置：2" in message for message in notifications)


def test_clear_completed_jobs_keeps_failed_and_cancelled(gui_app_module, tmp_path) -> None:
    service = gui_app_module.PersistentQueueService(tmp_path / "queue.json")
    states = {
        "done": gui_app_module.QueueState.COMPLETED,
        "failed": gui_app_module.QueueState.FAILED,
        "cancelled": gui_app_module.QueueState.CANCELLED,
    }
    for job_id, state in states.items():
        service.add_job(QueueJobSpec(job_id=job_id, job_type="analyze", project_name=job_id))
        service.update_job_state(job_id, state)

    controller = gui_app_module.WorkspaceController(str(REPO_ROOT / "config.ini.example"))
    controller._queue_service = service

    controller.clear_completed_jobs()

    assert service.get_job("done") is None
    assert service.get_job("failed") is not None
    assert service.get_job("cancelled") is not None


def test_persist_config_writes_and_applies_mineru_settings(gui_app_module, monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text((REPO_ROOT / "config.ini.example").read_text(encoding="utf-8"), encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("AUTO_GENERATE_ENV_PATH", str(env_path))
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    monkeypatch.setenv("MINERU_BASE_URL", "")
    monkeypatch.setenv("MINERU_MODEL_VERSION", "")
    monkeypatch.setenv("ALLOW_LOCAL_PARSE_FALLBACK", "true")

    controller = gui_app_module.WorkspaceController(str(config_path))
    controller.state["preprocess"]["parser_mode"] = "remote_first"
    controller.state["preprocess"]["primary_parser"] = "mineru_remote"
    controller.state["preprocess"]["fallback_parser"] = "local"
    controller.state["mineru"]["base_url"] = "https://mineru.example/api/v4"
    controller.state["mineru"]["api_token"] = "token-123"
    controller.state["mineru"]["model_version"] = "vlm-pro"
    controller.state["mineru"]["allow_local_parse_fallback"] = False

    controller.persist_config(notify_user=False)

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    assert parser["Preprocess"]["parser_mode"] == "remote_first"
    assert parser["Preprocess"]["primary_parser"] == "mineru_remote"
    assert parser["Preprocess"]["fallback_parser"] == "local"

    env_content = env_path.read_text(encoding="utf-8")
    assert "MINERU_BASE_URL=https://mineru.example/api/v4" in env_content
    assert "MINERU_API_TOKEN=token-123" in env_content
    assert "MINERU_MODEL_VERSION=vlm-pro" in env_content
    assert "ALLOW_LOCAL_PARSE_FALLBACK=false" in env_content

    assert controller.env_values["MINERU_API_TOKEN"] == "token-123"
    assert os.environ["MINERU_API_TOKEN"] == "token-123"
