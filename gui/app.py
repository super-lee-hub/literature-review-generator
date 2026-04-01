"""NiceGUI-powered local workspace for auto-generate."""

from __future__ import annotations

import asyncio
import configparser
import os
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from free_mode.profile_manager import get_profile_path, normalize_profile
from free_mode.service import generate_free_mode_profile, plan_free_mode_chat_turn
from services.configuration_service import (
    API_ENV_MAPPING,
    PROVIDER_PRESETS,
    ensure_config_sections,
    normalize_api_base,
    normalize_for_save,
    read_env_file,
    save_config_and_env,
    test_api_endpoint,
)
from services.environment_service import (
    RuntimeEnvironment,
    detect_runtime_environment,
    recommended_conda_activate_command,
    recommended_conda_create_command,
)
from services.workflow_facade import build_args, run_dispatch
from services.progress_service import ProgressTracker
from gui.i18n import LANGUAGE_OPTIONS, action_label, translate

try:
    from nicegui import ui  # pyright: ignore[reportMissingImports]
except ImportError as exc:  # pragma: no cover - optional dependency.
    raise RuntimeError("NiceGUI is not installed. Please install dependencies from requirements.txt.") from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8098
BUILD_STAMP = datetime.fromtimestamp(Path(__file__).stat().st_mtime).strftime("%Y-%m-%d %H:%M")

NAV_GROUPS = [
    {
        "title": "开始",
        "items": [
            ("总览", "/", "项目入口与推荐流程", "home"),
            ("核心工作台", "/workflow", "分析、写作与一键运行", "dashboard_customize"),
        ],
    },
    {
        "title": "配置",
        "items": [
            ("环境与路径", "/setup", "首次使用和基础 setup", "settings_suggest"),
            ("API 与模型", "/setup/api", "阅读、写作、大纲和验证模型", "hub"),
            ("性能与预处理", "/setup/processing", "并发、OCR、缓存与 RAG", "tune"),
        ],
    },
    {
        "title": "结果",
        "items": [
            ("日志与产物", "/logs", "查看状态、日志和输出目录", "receipt_long"),
            ("使用引导", "/guide", "给第一次使用的人看的说明", "menu_book"),
        ],
    },
]

SEARCH_ITEMS = [
    {
        "route": "/workflow",
        "label": "核心工作台",
        "keywords": ["工作台", "workflow", "run", "analyze", "outline", "review", "自由模式", "free mode", "概念", "concept"],
    },
    {
        "route": "/setup",
        "label": "环境与路径",
        "keywords": ["setup", "路径", "path", "zotero", "output", "输出"],
    },
    {
        "route": "/setup/api",
        "label": "API 与模型",
        "keywords": ["api", "model", "模型", "reader", "writer", "outline api", "validator"],
    },
    {
        "route": "/setup/processing",
        "label": "性能与预处理",
        "keywords": ["ocr", "preprocess", "rag", "cache", "缓存", "预处理", "并发", "validation"],
    },
    {
        "route": "/logs",
        "label": "日志与产物",
        "keywords": ["log", "logs", "日志", "output", "产物", "失败", "report"],
    },
    {
        "route": "/guide",
        "label": "使用引导",
        "keywords": ["guide", "help", "帮助", "怎么用", "新手", "first time"],
    },
]

STYLE_BLOCK = """
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<style>
:root {
  --paper: #f5f1e8;
  --paper-soft: #efe8dc;
  --panel: rgba(255, 252, 247, 0.78);
  --panel-strong: rgba(255, 252, 247, 0.92);
  --ink: #202725;
  --muted: #60706a;
  --accent: #5b6d66;
  --accent-soft: #dde5df;
  --line: rgba(32, 39, 37, 0.10);
  --line-strong: rgba(32, 39, 37, 0.16);
  --shadow: 0 24px 48px rgba(31, 37, 35, 0.08);
}
body, .nicegui-content {
  background:
    radial-gradient(circle at top left, rgba(202, 216, 208, 0.45), transparent 28%),
    radial-gradient(circle at bottom right, rgba(229, 221, 207, 0.45), transparent 30%),
    linear-gradient(180deg, var(--paper), #f7f3ec 55%, var(--paper-soft));
  color: var(--ink);
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
.ag-topbar,
.ag-fixedbar {
  background: rgba(245, 241, 232, 0.78);
  backdrop-filter: blur(18px);
  border-bottom: 1px solid var(--line);
}
.ag-fixedbar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  padding: 12px 18px 10px 18px;
}
.ag-fixedbar,
.ag-fixedbar *,
.ag-topbar,
.ag-topbar * {
  color: var(--ink) !important;
}
.ag-topbar-title {
  color: #50675f !important;
  font-weight: 700;
  font-size: 1.28rem;
  letter-spacing: 0.045em;
  font-family: "Palatino Linotype", Georgia, "Times New Roman", serif;
  font-variant: small-caps;
}
.ag-title-stack {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: max-content;
}
.ag-build-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.62);
  border: 1px solid var(--line);
  color: var(--muted) !important;
  font-size: 0.78rem;
  line-height: 1;
  white-space: nowrap;
}
.ag-fixedbar-shell {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}
.ag-fixedbar-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
}
.ag-fixedbar-tools {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ag-search {
  min-width: 360px;
  flex: 1 1 420px;
}
.ag-search .q-field__control {
  background: rgba(255, 255, 255, 0.72);
  border-radius: 16px;
  border: 1px solid var(--line-strong);
}
.ag-search .q-field__native,
.ag-search input {
  color: var(--ink) !important;
}
.ag-search-button {
  min-height: 44px;
}
.ag-reminder {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-radius: 14px;
  background: rgba(221, 229, 223, 0.88);
  border: 1px solid var(--line-strong);
  color: var(--ink) !important;
  font-weight: 500;
  width: 100%;
}
.ag-reminder-text {
  color: var(--ink) !important;
  line-height: 1.55;
}
.ag-inline-alert {
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border-radius: 16px;
  border: 1px solid var(--line-strong);
  background: rgba(255, 255, 255, 0.66);
}
.ag-inline-alert-positive {
  background: rgba(221, 235, 228, 0.9);
}
.ag-inline-alert-negative {
  background: rgba(244, 224, 222, 0.92);
}
.ag-inline-alert-warning {
  background: rgba(245, 234, 208, 0.92);
}
.ag-inline-alert-info {
  background: rgba(224, 233, 235, 0.92);
}
.ag-fixedbar .q-btn,
.ag-topbar .q-btn {
  background: rgba(255, 255, 255, 0.64);
  border: 1px solid var(--line);
}
.ag-fixedbar .q-btn.q-btn--outline,
.ag-topbar .q-btn.q-btn--outline {
  background: rgba(255, 255, 255, 0.38);
  border-color: var(--line-strong);
}
.ag-drawer {
  background: rgba(249, 246, 240, 0.88);
  backdrop-filter: blur(20px);
  border-right: 1px solid var(--line);
}
.ag-page {
  max-width: 1380px;
  min-height: calc(100vh - 132px);
  margin: 180px auto 40px auto;
  padding: 0 24px 32px 24px;
}
.ag-page-reminder {
  margin-bottom: 6px;
}
.ag-page-head {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 22px;
}
.ag-page-title {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 2rem;
  letter-spacing: 0.02em;
  margin: 0;
}
.ag-page-subtitle {
  color: var(--muted);
  line-height: 1.7;
  max-width: 880px;
}
.ag-card, .q-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 24px;
  box-shadow: var(--shadow);
  backdrop-filter: blur(12px);
}
.ag-card-strong {
  background: var(--panel-strong);
}
.ag-section-title {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.12rem;
  line-height: 1.5;
}
.ag-subtle {
  color: var(--muted);
  line-height: 1.75;
}
.ag-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--ink);
  font-size: 0.82rem;
  width: fit-content;
}
.ag-kpi {
  font-size: 1.4rem;
  font-weight: 600;
}
.ag-nav-group {
  margin-bottom: 18px;
}
.ag-nav-title {
  color: var(--muted);
  font-size: 0.76rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  margin: 0 0 8px 0;
}
.ag-nav-link {
  display: block;
  text-decoration: none;
  color: inherit;
  border: 1px solid transparent;
  border-radius: 18px;
  padding: 12px 12px;
  transition: all 0.18s ease;
  margin-bottom: 8px;
}
.ag-nav-link:hover {
  background: rgba(255, 255, 255, 0.55);
  border-color: var(--line);
}
.ag-nav-link-active {
  background: rgba(255, 255, 255, 0.68);
  border-color: var(--line-strong);
}
.ag-nav-label {
  font-weight: 600;
}
.ag-nav-desc {
  color: var(--muted);
  font-size: 0.84rem;
  line-height: 1.45;
}
.ag-status {
  padding: 8px 12px;
  border-radius: 14px;
  background: rgba(221, 229, 223, 0.92);
  border: 1px solid var(--line-strong);
  color: var(--ink);
  font-weight: 600;
}
.ag-status-text {
  color: var(--ink) !important;
}
.ag-hero {
  min-height: 236px;
}
.ag-grid-2 {
  display: grid;
  grid-template-columns: 1.45fr 1fr;
  gap: 18px;
}
.ag-grid-3 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px;
}
.ag-grid-compact {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}
.ag-card-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
}
.ag-card-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: auto;
}
.ag-mini-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}
.ag-mini-card {
  padding: 16px;
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.55);
  border: 1px solid var(--line);
}
.ag-checklist {
  display: grid;
  gap: 12px;
}
.ag-check-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.55);
  border: 1px solid var(--line);
}
.ag-workflow-shell {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(320px, 0.95fr);
  gap: 18px;
  align-items: start;
}
.ag-sidebar-stack {
  display: flex;
  flex-direction: column;
  gap: 18px;
  position: sticky;
  top: 186px;
}
.ag-mode-grid {
  display: grid;
  grid-template-columns: 0.95fr 1.35fr;
  gap: 14px;
}
.ag-action-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.ag-action-tile {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.55);
  border: 1px solid var(--line);
  min-height: 188px;
}
.ag-action-tile .q-btn {
  margin-top: auto;
}
.ag-button-column {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
@media (max-width: 1100px) {
  .ag-grid-2, .ag-grid-3, .ag-grid-compact, .ag-mini-grid, .ag-workflow-shell, .ag-mode-grid, .ag-action-grid {
    grid-template-columns: 1fr;
  }
  .ag-sidebar-stack {
    position: static;
  }
  .ag-fixedbar-main {
    flex-direction: column;
    align-items: stretch;
  }
  .ag-fixedbar-tools {
    width: 100%;
  }
  .ag-search {
    min-width: 100%;
  }
}
</style>
"""


def _read_existing_config(config_path: str) -> Dict[str, Dict[str, str]]:
    parser = configparser.ConfigParser()
    if os.path.exists(config_path):
        parser.read(config_path, encoding="utf-8")
    existing = {section: dict(parser[section]) for section in parser.sections()}
    return ensure_config_sections(existing)


def _guess_provider(api_base: str) -> str:
    normalized = normalize_api_base(api_base or "", provider="custom")
    for key, preset in PROVIDER_PRESETS.items():
        if preset.default_api_base and normalized.startswith(preset.default_api_base.rstrip("/")):
            return key
    return "custom"


def _latest_log_excerpt(language: str = "zh-CN") -> Tuple[str, str]:
    logs_dir = REPO_ROOT / "logs"
    if not logs_dir.exists():
        return "", translate(language, "暂无日志文件。")

    log_files = sorted(logs_dir.glob("*.log"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not log_files:
        return "", translate(language, "暂无日志文件。")

    latest = log_files[0]
    try:
        lines = latest.read_text(encoding="utf-8", errors="ignore").splitlines()
        excerpt = "\n".join(lines[-60:])
        return str(latest), excerpt
    except Exception as exc:  # pragma: no cover - defensive.
        return str(latest), translate(language, "无法读取日志：{exc}").format(exc=exc)


def _open_path(path: str, language: str = "zh-CN") -> None:
    if not path:
        ui.notify(translate(language, "路径为空。"), color="warning")
        return
    target = os.path.abspath(path)
    if not os.path.exists(target):
        ui.notify(translate(language, "路径不存在：{target}").format(target=target), color="warning")
        return
    if os.environ.get("AUTO_GENERATE_GUI_TEST_MODE", "").lower() in {"1", "true", "yes"}:
        ui.notify(translate(language, "测试模式：已模拟打开路径 {target}").format(target=target), color="info")
        return
    os.startfile(target)  # type: ignore[attr-defined]


def _count_log_files() -> int:
    logs_dir = REPO_ROOT / "logs"
    return len(list(logs_dir.glob("*.log"))) if logs_dir.exists() else 0


def _environment_ui_copy(language: str, env_info: RuntimeEnvironment) -> Dict[str, str]:
    create_command = recommended_conda_create_command()
    activate_command = recommended_conda_activate_command()

    if language == "en":
        if env_info.kind == "conda" and not env_info.is_base_conda:
            env_type = "Dedicated conda environment"
            isolation = "Isolated"
            recommendation = "This interpreter is already running inside an isolated conda environment, which is the recommended setup for this project."
        elif env_info.kind == "conda" and env_info.is_base_conda:
            env_type = "Conda base environment"
            isolation = "Needs isolation"
            recommendation = "You are currently running inside conda base. It is safer to create a dedicated environment so this project's packages do not clash with other tools."
        elif env_info.kind == "venv":
            env_type = "venv / virtualenv"
            isolation = "Isolated"
            recommendation = "This interpreter is already isolated via venv or virtualenv. That is fine. If your team prefers conda, you can also create a dedicated conda environment."
        else:
            env_type = "Global / non-isolated Python"
            isolation = "Needs isolation"
            recommendation = "No dedicated environment was detected. It is strongly recommended to create a separate conda environment before installing dependencies."

        return {
            "title": "Current Runtime Environment",
            "intro": "The GUI can detect the current Python runtime for you. If you are in conda base or a global interpreter, create a dedicated environment before continuing.",
            "env_type_label": "Environment Type",
            "env_name_label": "Environment Name",
            "isolation_label": "Isolation Status",
            "executable_label": "Interpreter Path",
            "env_type": env_type,
            "env_name": env_info.name,
            "isolation": isolation,
            "executable": env_info.executable,
            "recommendation": recommendation,
            "command_title": "Recommended conda commands",
            "create_command": create_command,
            "activate_command": activate_command,
        }

    if env_info.kind == "conda" and not env_info.is_base_conda:
        env_type = "Conda 独立环境"
        isolation = "已隔离"
        recommendation = "当前解释器已经在独立 conda 环境里运行，这正是这个项目最推荐的使用方式。"
    elif env_info.kind == "conda" and env_info.is_base_conda:
        env_type = "Conda base 环境"
        isolation = "建议独立环境"
        recommendation = "你现在是在 conda base 里运行。更稳妥的做法是单独建一个环境，再安装这个项目的依赖，避免和你原来的包互相影响。"
    elif env_info.kind == "venv":
        env_type = "venv / virtualenv"
        isolation = "已隔离"
        recommendation = "当前解释器已经在 venv / virtualenv 中，也属于隔离环境。如果你更习惯 conda，也可以换成独立 conda 环境。"
    else:
        env_type = "全局 / 未隔离 Python"
        isolation = "建议独立环境"
        recommendation = "当前没有检测到独立环境。建议先创建单独的 conda 环境，再安装依赖，能明显减少包冲突和后续排错成本。"

    return {
        "title": "当前运行环境",
        "intro": "GUI 会自动识别你当前使用的 Python / conda 环境。如果你现在在 base 或全局环境里，建议先切到独立环境再继续。",
        "env_type_label": "环境类型",
        "env_name_label": "环境名称",
        "isolation_label": "隔离状态",
        "executable_label": "解释器路径",
        "env_type": env_type,
        "env_name": env_info.name,
        "isolation": isolation,
        "executable": env_info.executable,
        "recommendation": recommendation,
        "command_title": "推荐的 conda 命令",
        "create_command": create_command,
        "activate_command": activate_command,
    }


def _open_native_path_dialog(
    *,
    pick: str,
    initial_path: str = "",
    title: str = "",
    filetypes: Iterable[tuple[str, str]] | None = None,
) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return ""

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    root.update()

    normalized = os.path.abspath(initial_path) if initial_path else str(REPO_ROOT)
    initial_dir = normalized if os.path.isdir(normalized) else os.path.dirname(normalized) or str(REPO_ROOT)

    try:
        if pick == "directory":
            selected = filedialog.askdirectory(initialdir=initial_dir, title=title or "Select Folder")
        else:
            selected = filedialog.askopenfilename(
                initialdir=initial_dir,
                title=title or "Select File",
                filetypes=list(filetypes or [("All Files", "*.*")]),
            )
    finally:
        root.destroy()

    return str(selected or "")


@dataclass
class UiBindings:
    status_labels: list[Any] = field(default_factory=list)
    log_path_labels: list[Any] = field(default_factory=list)
    log_views: list[Any] = field(default_factory=list)
    action_buttons: list[Any] = field(default_factory=list)
    free_mode_send_buttons: list[Any] = field(default_factory=list)
    free_mode_apply_buttons: list[Any] = field(default_factory=list)
    free_mode_reset_buttons: list[Any] = field(default_factory=list)
    free_mode_transcript_views: list[Any] = field(default_factory=list)
    free_mode_profile_views: list[Any] = field(default_factory=list)
    free_mode_status_labels: list[Any] = field(default_factory=list)
    free_mode_hint_labels: list[Any] = field(default_factory=list)
    api_feedback_boxes: Dict[str, Any] = field(default_factory=dict)
    api_feedback_labels: Dict[str, Any] = field(default_factory=dict)
    api_feedback_icons: Dict[str, Any] = field(default_factory=dict)
    progress_task_labels: list[Any] = field(default_factory=list)
    progress_stage_labels: list[Any] = field(default_factory=list)
    progress_message_labels: list[Any] = field(default_factory=list)
    progress_item_labels: list[Any] = field(default_factory=list)
    progress_counts_labels: list[Any] = field(default_factory=list)
    progress_retry_labels: list[Any] = field(default_factory=list)
    progress_elapsed_labels: list[Any] = field(default_factory=list)
    progress_overall_bars: list[Any] = field(default_factory=list)
    progress_stage_bars: list[Any] = field(default_factory=list)


class WorkspaceController:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.env_path = os.environ.get("AUTO_GENERATE_ENV_PATH", str(REPO_ROOT / ".env"))
        self.test_mode = os.environ.get("AUTO_GENERATE_GUI_TEST_MODE", "").lower() in {"1", "true", "yes"}
        self.runtime_environment = detect_runtime_environment()
        self.client: Any | None = None
        self.sections = _read_existing_config(config_path)
        self.env_values = read_env_file(self.env_path)
        self.language = self.sections.get("GUI", {}).get("language", "zh-CN")
        if self.language not in LANGUAGE_OPTIONS:
            self.language = "zh-CN"
        self.bindings = UiBindings()
        self.search_query = ""
        self.latest_log_path, self.latest_log_excerpt = _latest_log_excerpt(self.language)
        self.progress_tracker: Optional[ProgressTracker] = None
        self.progress_snapshot: Dict[str, Any] = ProgressTracker().snapshot()
        self.workflow_running = False
        self.free_mode_chat_input = ""
        self.free_mode_messages: list[Dict[str, str]] = []
        self.free_mode_profile_draft: Dict[str, Any] = normalize_profile(None)
        self.free_mode_missing_information: list[str] = []
        self.free_mode_profile_path = ""
        self.free_mode_ready_to_apply = False
        self.free_mode_busy = False
        self.status_message = f'{self.t("工作台已就绪。建议先进入“环境与路径”完成 setup，再回到“核心工作台”运行流程。")}  Build {BUILD_STAMP}'
        self.state: Dict[str, Any] = {
            "paths": {
                "zotero_report": self.sections["Paths"].get("zotero_report", ""),
                "library_path": self.sections["Paths"].get("library_path", ""),
                "output_path": self.sections["Paths"].get("output_path", "./output"),
            },
            "performance": {
                "max_workers": self.sections["Performance"].get("max_workers", "3"),
                "api_retry_attempts": self.sections["Performance"].get("api_retry_attempts", "5"),
                "enable_stage1_validation": self.sections["Performance"].get("enable_stage1_validation", "false") == "true",
                "enable_stage2_validation": self.sections["Performance"].get("enable_stage2_validation", "false") == "true",
            },
            "stage2_retry": {
                "enabled": self.sections.get("Stage2_Retry", {}).get("enabled", "true") == "true",
                "max_retry_rounds": self.sections.get("Stage2_Retry", {}).get("max_retry_rounds", "2"),
                "base_retry_delay": self.sections.get("Stage2_Retry", {}).get("base_retry_delay", "30"),
                "max_retry_delay": self.sections.get("Stage2_Retry", {}).get("max_retry_delay", "120"),
            },
            "preprocess": {
                "enabled": self.sections["Preprocess"].get("enabled", "true") == "true",
                "cache_dir": self.sections["Preprocess"].get("cache_dir", "./output/_preprocess_cache"),
                "extractor_profile": self.sections["Preprocess"].get("extractor_profile", "auto"),
                "ocr_mode": self.sections["Preprocess"].get("ocr_mode", "auto"),
                "ocr_languages": self.sections["Preprocess"].get("ocr_languages", "eng"),
                "force_rebuild": self.sections["Preprocess"].get("force_rebuild", "false") == "true",
                "enable_local_rag": self.sections["Preprocess"].get("enable_local_rag", "false") == "true",
                "rag_backend": self.sections["Preprocess"].get("rag_backend", "chroma"),
            },
            "workflow": {
                "project_name": "",
                "pdf_folder": "",
                "concept": "",
                "free_mode_idea": "",
                "section_number": "1",
            },
        }
        self.api_cards: Dict[str, Dict[str, str]] = {}
        for section_name in API_ENV_MAPPING:
            api_base = self.sections.get(section_name, {}).get("api_base", "")
            self.api_cards[section_name] = {
                "provider": _guess_provider(api_base),
                "model": self.sections.get(section_name, {}).get("model", ""),
                "api_base": api_base,
                "api_key": self.env_values.get(API_ENV_MAPPING[section_name], ""),
            }

    def t(self, key: str) -> str:
        return translate(self.language, key)

    def tf(self, key: str, **kwargs: Any) -> str:
        return self.t(key).format(**kwargs)

    def action_label(self, action: str) -> str:
        return action_label(self.language, action)

    def register_status_label(self, label: Any) -> None:
        self.bindings.status_labels.append(label)
        label.set_text(self.status_message)

    def register_client(self, client: Any) -> None:
        self.client = client

    def notify(
        self,
        message: str,
        *,
        color: str = "positive",
        multi_line: bool = False,
        close_button: bool | str = True,
    ) -> None:
        if self.client is None:
            return
        with self.client:
            ui.notify(message, color=color, multi_line=multi_line, close_button=close_button)

    def register_log_widgets(self, path_label: Any, log_view: Any) -> None:
        self.bindings.log_path_labels.append(path_label)
        self.bindings.log_views.append(log_view)
        path_label.set_text(self.latest_log_path or self.t("暂无日志文件。"))
        log_view.set_value(self.latest_log_excerpt)

    def register_action_button(self, button: Any) -> None:
        self.bindings.action_buttons.append(button)
        if self.workflow_running:
            button.disable()

    def register_free_mode_widgets(
        self,
        *,
        transcript_view: Any,
        profile_view: Any,
        status_label: Any,
        hint_label: Any,
        send_button: Any,
        apply_button: Any,
        reset_button: Any,
    ) -> None:
        self.bindings.free_mode_transcript_views.append(transcript_view)
        self.bindings.free_mode_profile_views.append(profile_view)
        self.bindings.free_mode_status_labels.append(status_label)
        self.bindings.free_mode_hint_labels.append(hint_label)
        self.bindings.free_mode_send_buttons.append(send_button)
        self.bindings.free_mode_apply_buttons.append(apply_button)
        self.bindings.free_mode_reset_buttons.append(reset_button)
        self.update_free_mode_widgets()

    def register_progress_widgets(
        self,
        *,
        task_label: Any,
        stage_label: Any,
        message_label: Any,
        item_label: Any,
        counts_label: Any,
        retry_label: Any,
        elapsed_label: Any,
        overall_bar: Any,
        stage_bar: Any,
    ) -> None:
        self.bindings.progress_task_labels.append(task_label)
        self.bindings.progress_stage_labels.append(stage_label)
        self.bindings.progress_message_labels.append(message_label)
        self.bindings.progress_item_labels.append(item_label)
        self.bindings.progress_counts_labels.append(counts_label)
        self.bindings.progress_retry_labels.append(retry_label)
        self.bindings.progress_elapsed_labels.append(elapsed_label)
        self.bindings.progress_overall_bars.append(overall_bar)
        self.bindings.progress_stage_bars.append(stage_bar)
        self.update_progress_widgets()

    def register_api_feedback(self, section_name: str, box: Any, label: Any, icon: Any) -> None:
        self.bindings.api_feedback_boxes[section_name] = box
        self.bindings.api_feedback_labels[section_name] = label
        self.bindings.api_feedback_icons[section_name] = icon

    def hide_api_feedback(self, section_name: str) -> None:
        box = self.bindings.api_feedback_boxes.get(section_name)
        if box is not None:
            box.classes(add="hidden", remove="flex")

    def show_api_feedback(self, section_name: str, message: str, *, tone: str = "info") -> None:
        box = self.bindings.api_feedback_boxes.get(section_name)
        label = self.bindings.api_feedback_labels.get(section_name)
        icon = self.bindings.api_feedback_icons.get(section_name)
        if box is None or label is None or icon is None:
            return

        for klass in ("ag-inline-alert-positive", "ag-inline-alert-negative", "ag-inline-alert-warning", "ag-inline-alert-info"):
            box.classes(remove=klass)
        box.classes(remove="hidden", add=f"flex ag-inline-alert-{tone}")
        label.set_text(message)
        icon.name = {
            "positive": "task_alt",
            "negative": "error",
            "warning": "warning",
            "info": "info",
        }.get(tone, "info")

    def set_status(self, message: str) -> None:
        self.status_message = message
        for label in self.bindings.status_labels:
            label.set_text(message)

    def set_workflow_running(self, running: bool) -> None:
        self.workflow_running = running
        for button in self.bindings.action_buttons:
            if running:
                button.disable()
            else:
                button.enable()
        self.update_free_mode_widgets()

    @staticmethod
    def _profile_has_content(profile: Dict[str, Any]) -> bool:
        for value in profile.values():
            if isinstance(value, list) and value:
                return True
            if isinstance(value, str) and value.strip():
                return True
        return False

    def _format_free_mode_transcript(self) -> str:
        if not self.free_mode_messages:
            return self.t("自由模式会先和你多轮澄清写作意图，再把对话整理成可执行的 prompt profile。")

        lines: list[str] = []
        for message in self.free_mode_messages:
            role = self.t("你") if message.get("role") == "user" else self.t("规划助手")
            content = str(message.get("content", "")).strip()
            if content:
                lines.append(f"{role}：{content}")
        return "\n\n".join(lines)

    def _format_free_mode_profile(self) -> str:
        profile = normalize_profile(self.free_mode_profile_draft)
        if not self._profile_has_content(profile):
            return self.t("对话过程中提炼出的研究目标、概念关系、关注重点和优化 prompt 会显示在这里。")

        def format_list(values: list[str]) -> str:
            return "\n".join(f"- {item}" for item in values) if values else "-"

        parts = [
            f"{self.t('研究目标')}：{profile['research_goal'] or '-'}",
            f"{self.t('概念关系 / 主线')}：{profile['concept_relationship'] or '-'}",
            f"{self.t('关注重点')}：\n{format_list(profile['focus_points'])}",
            f"{self.t('排除项')}：\n{format_list(profile['exclusions'])}",
            f"{self.t('理论 / 变量焦点')}：\n{format_list(profile['theory_or_variable_focus'])}",
            f"{self.t('结构偏好')}：\n{format_list(profile['outline_preferences'])}",
            f"{self.t('写作约束')}：\n{format_list(profile['writing_constraints'])}",
            f"{self.t('生成后的优化 prompt')}：\n{profile['generated_prompt'] or '-'}",
            f"{self.t('对话摘要')}：\n{format_list(profile['conversation_notes'])}",
        ]
        return "\n\n".join(parts)

    def _free_mode_status_text(self) -> str:
        if self.free_mode_busy:
            return self.t("自由模式正在整理你的想法…")
        if self.free_mode_profile_path:
            return self.tf("自由模式已应用到本次任务：{target}", target=self.free_mode_profile_path)
        if self.free_mode_ready_to_apply and self.free_mode_messages:
            return self.t("当前规划已经比较完整，可以直接应用到本次任务。")
        if self.free_mode_messages:
            return self.t("当前规划还在澄清阶段，你可以继续补充，也可以先应用草案。")
        return self.t("先告诉规划助手你想写什么，它会边聊边帮你收束成适合综述流程的 prompt。")

    def _free_mode_hint_text(self) -> str:
        if self.free_mode_busy:
            return self.t("本轮对话返回后，这里会更新仍需补充的信息。")
        if self.free_mode_missing_information:
            return self.tf("还建议再确认这些点：{items}", items="；".join(self.free_mode_missing_information))
        if self.free_mode_profile_path:
            return self.t("后续运行会优先使用这份已应用的自由模式 profile。")
        return self.t("例如：我想围绕概念 A 如何推导到概念 B 来写综述，重点比较变量链路、理论解释和 research gap。")

    def update_free_mode_widgets(self) -> None:
        transcript_text = self._format_free_mode_transcript()
        profile_text = self._format_free_mode_profile()
        status_text = self._free_mode_status_text()
        hint_text = self._free_mode_hint_text()
        disable_controls = self.workflow_running or self.free_mode_busy
        can_apply = (not disable_controls) and bool(self.free_mode_messages)

        for view in self.bindings.free_mode_transcript_views:
            view.set_value(transcript_text)
        for view in self.bindings.free_mode_profile_views:
            view.set_value(profile_text)
        for label in self.bindings.free_mode_status_labels:
            label.set_text(status_text)
        for label in self.bindings.free_mode_hint_labels:
            label.set_text(hint_text)
        for button in self.bindings.free_mode_send_buttons + self.bindings.free_mode_reset_buttons:
            if disable_controls:
                button.disable()
            else:
                button.enable()
        for button in self.bindings.free_mode_apply_buttons:
            if can_apply:
                button.enable()
            else:
                button.disable()

    def _collect_config_payload(self) -> tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
        updated_sections = ensure_config_sections(self.sections)
        updated_sections["Paths"].update(
            {
                "zotero_report": self.state["paths"]["zotero_report"],
                "library_path": self.state["paths"]["library_path"],
                "output_path": self.state["paths"]["output_path"],
            }
        )
        updated_sections["Performance"].update(
            {
                "max_workers": str(self.state["performance"]["max_workers"]),
                "api_retry_attempts": str(self.state["performance"]["api_retry_attempts"]),
                "enable_stage1_validation": "true" if self.state["performance"]["enable_stage1_validation"] else "false",
                "enable_stage2_validation": "true" if self.state["performance"]["enable_stage2_validation"] else "false",
            }
        )
        updated_sections["Stage2_Retry"].update(
            {
                "enabled": "true" if self.state["stage2_retry"]["enabled"] else "false",
                "max_retry_rounds": str(self.state["stage2_retry"]["max_retry_rounds"]),
                "base_retry_delay": str(self.state["stage2_retry"]["base_retry_delay"]),
                "max_retry_delay": str(self.state["stage2_retry"]["max_retry_delay"]),
            }
        )
        updated_sections["Preprocess"].update(
            {
                "enabled": "true" if self.state["preprocess"]["enabled"] else "false",
                "cache_dir": self.state["preprocess"]["cache_dir"],
                "extractor_profile": self.state["preprocess"]["extractor_profile"],
                "ocr_mode": self.state["preprocess"]["ocr_mode"],
                "ocr_languages": self.state["preprocess"]["ocr_languages"],
                "force_rebuild": "true" if self.state["preprocess"]["force_rebuild"] else "false",
                "enable_local_rag": "true" if self.state["preprocess"]["enable_local_rag"] else "false",
                "rag_backend": self.state["preprocess"]["rag_backend"],
            }
        )
        updated_sections.setdefault("GUI", {})
        updated_sections["GUI"]["language"] = self.language

        api_keys: Dict[str, str] = {}
        for section_name, card in self.api_cards.items():
            updated_sections.setdefault(section_name, {})
            updated_sections[section_name]["provider"] = card["provider"]
            updated_sections[section_name]["model"] = card["model"]
            updated_sections[section_name]["api_base"] = card["api_base"]
            api_keys[section_name] = card["api_key"]

        return updated_sections, api_keys

    def build_runtime_config(self) -> Dict[str, Dict[str, str]]:
        runtime_sections, api_keys = self._collect_config_payload()
        normalize_for_save(runtime_sections)
        runtime_config = ensure_config_sections(runtime_sections)
        for section_name, api_key in api_keys.items():
            runtime_config.setdefault(section_name, {})
            runtime_config[section_name]["api_key"] = api_key
        return runtime_config

    def _mock_free_mode_response(self, user_message: str) -> Dict[str, Any]:
        profile = normalize_profile(
            {
                "research_goal": user_message[:80],
                "concept_relationship": "围绕用户刚刚描述的概念关系继续规划",
                "focus_points": ["变量链路", "理论解释", "research gap"],
                "outline_preferences": ["先界定概念，再展开推导逻辑"],
                "generated_prompt": f"请围绕以下研究意图组织综述：{user_message}",
                "conversation_notes": [user_message],
            }
        )
        return {
            "assistant_message": "我先把这轮想法整理进规划草案里了。你可以继续补充边界条件、理论视角或更想强调的章节主线。",
            "ready_to_apply": True,
            "missing_information": ["是否需要限定研究场景或样本类型"],
            "profile": profile,
        }

    async def send_free_mode_message(self) -> None:
        message = self.free_mode_chat_input.strip()
        if not message:
            self.notify(self.t("请先输入你想和自由模式讨论的内容。"), color="warning")
            return
        if self.free_mode_busy or self.workflow_running:
            self.notify(self.t("当前有任务正在占用工作台，请稍后再继续自由模式对话。"), color="info")
            return

        self.free_mode_messages.append({"role": "user", "content": message})
        self.free_mode_chat_input = ""
        self.free_mode_busy = True
        self.free_mode_profile_path = ""
        self.free_mode_ready_to_apply = False
        self.update_free_mode_widgets()

        if self.test_mode:
            response = self._mock_free_mode_response(message)
        else:
            response = await asyncio.to_thread(
                plan_free_mode_chat_turn,
                messages=list(self.free_mode_messages),
                config=self.build_runtime_config(),
            )

        self.free_mode_busy = False
        if not response:
            self.set_status(self.t("自由模式本轮规划失败，请检查 Free_Mode_API / Outline_API 配置后重试。"))
            self.notify(self.status_message, color="negative", multi_line=True)
            self.update_free_mode_widgets()
            return

        self.free_mode_messages.append({"role": "assistant", "content": str(response.get("assistant_message", "")).strip()})
        self.free_mode_profile_draft = normalize_profile(response.get("profile"))
        self.free_mode_missing_information = [str(item).strip() for item in response.get("missing_information", []) if str(item).strip()]
        self.free_mode_ready_to_apply = bool(response.get("ready_to_apply"))
        self.set_status(self.t("自由模式规划已更新，你可以继续追问，或把当前规划应用到本次任务。"))
        self.update_free_mode_widgets()

    def clear_free_mode_planner(self) -> None:
        if self.free_mode_busy:
            return
        self.free_mode_chat_input = ""
        self.free_mode_messages = []
        self.free_mode_profile_draft = normalize_profile(None)
        self.free_mode_missing_information = []
        self.free_mode_profile_path = ""
        self.free_mode_ready_to_apply = False
        self.update_free_mode_widgets()
        self.notify(self.t("自由模式对话已清空。"), color="positive")

    async def apply_free_mode_plan(self) -> None:
        if self.free_mode_busy or self.workflow_running:
            self.notify(self.t("当前有任务正在占用工作台，请稍后再应用自由模式规划。"), color="info")
            return
        if not self.free_mode_messages:
            self.notify(self.t("请先和自由模式对话，再决定是否应用到本次任务。"), color="warning")
            return

        project_name = str(self.state["workflow"]["project_name"]).strip()
        if not project_name:
            self.notify(self.t("请先填写项目名，再应用自由模式规划。"), color="warning")
            return

        self.free_mode_busy = True
        self.update_free_mode_widgets()

        output_dir = str(self.state["paths"]["output_path"] or "./output")
        if self.test_mode:
            profile = normalize_profile(self.free_mode_profile_draft)
        else:
            profile = await asyncio.to_thread(
                generate_free_mode_profile,
                user_idea="",
                config=self.build_runtime_config(),
                output_dir=output_dir,
                project_name=project_name,
                conversation_messages=list(self.free_mode_messages),
            )

        self.free_mode_busy = False
        if not profile:
            self.set_status(self.t("自由模式 profile 应用失败，请检查 Free_Mode_API / Outline_API 配置后重试。"))
            self.notify(self.status_message, color="negative", multi_line=True)
            self.update_free_mode_widgets()
            return

        self.free_mode_profile_draft = normalize_profile(profile)
        self.free_mode_profile_path = get_profile_path(output_dir, project_name)
        self.free_mode_ready_to_apply = True
        self.set_status(self.tf("自由模式已应用到本次任务：{target}", target=self.free_mode_profile_path))
        self.notify(self.status_message, color="positive", multi_line=True)
        self.update_free_mode_widgets()

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total_seconds = max(0, int(seconds or 0))
        minutes, remaining = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{remaining:02d}"
        return f"{minutes:02d}:{remaining:02d}"

    def update_progress_widgets(self) -> None:
        snapshot = self.progress_snapshot
        status = str(snapshot.get("status") or "idle")
        task_text = snapshot.get("task_type") or self.t("暂无运行中的任务")
        stage_text = snapshot.get("stage") or "-"
        message_text = snapshot.get("message") or self.t("等待模型返回或执行长任务中…")
        item_text = snapshot.get("item_label") or "-"
        success_count = int(snapshot.get("success_count") or 0)
        failure_count = int(snapshot.get("failure_count") or 0)
        remaining_count = int(snapshot.get("remaining_count") or 0)
        retry_round = int(snapshot.get("retry_round") or 0)
        retry_total = int(snapshot.get("retry_total_rounds") or 0)
        elapsed_text = self._format_elapsed(float(snapshot.get("elapsed_seconds") or 0.0))
        counts_text = f"{success_count} / {failure_count} / {remaining_count}"
        retry_text = "-" if retry_total <= 0 else f"{retry_round}/{retry_total}"

        total = int(snapshot.get("total") or 0)
        current = int(snapshot.get("current") or 0)
        indeterminate = bool(snapshot.get("indeterminate"))
        if total <= 0:
            progress_value = 1.0 if status == "completed" else 0.0
        else:
            progress_value = min(max(current / total, 0.0), 1.0)
        show_indeterminate = status == "running" and (indeterminate or total <= 0)

        for label in self.bindings.progress_task_labels:
            label.set_text(task_text)
        for label in self.bindings.progress_stage_labels:
            label.set_text(stage_text)
        for label in self.bindings.progress_message_labels:
            label.set_text(message_text)
        for label in self.bindings.progress_item_labels:
            label.set_text(item_text)
        for label in self.bindings.progress_counts_labels:
            label.set_text(counts_text)
        for label in self.bindings.progress_retry_labels:
            label.set_text(retry_text)
        for label in self.bindings.progress_elapsed_labels:
            label.set_text(elapsed_text)

        for bar in self.bindings.progress_overall_bars + self.bindings.progress_stage_bars:
            if show_indeterminate:
                bar.props(add="indeterminate")
                bar.set_value(0)
            else:
                bar.props(remove="indeterminate")
                bar.set_value(progress_value)

    def refresh_progress(self) -> None:
        if self.progress_tracker is not None:
            self.progress_snapshot = self.progress_tracker.snapshot()
            if self.progress_snapshot.get("status") in {"completed", "failed"}:
                self.set_workflow_running(False)
        self.update_progress_widgets()
        if self.workflow_running:
            self.refresh_logs()

    def refresh_logs(self) -> None:
        self.latest_log_path, self.latest_log_excerpt = _latest_log_excerpt(self.language)
        for label in self.bindings.log_path_labels:
            label.set_text(self.latest_log_path or self.t("暂无日志文件。"))
        for log_view in self.bindings.log_views:
            log_view.set_value(self.latest_log_excerpt)

    def persist_config(self, *, notify_user: bool = True) -> None:
        updated_sections, api_keys = self._collect_config_payload()
        normalize_for_save(updated_sections)
        save_config_and_env(updated_sections, api_keys, config_path=self.config_path, env_path=self.env_path)
        self.sections = ensure_config_sections(updated_sections)
        self.set_status(self.tf("配置已保存到 {config_path} 和 {env_path}", config_path=self.config_path, env_path=self.env_path))
        if notify_user:
            self.notify(self.t("配置已保存。"), color="positive")

    def change_language(self, language: str) -> None:
        if language not in LANGUAGE_OPTIONS:
            return
        self.language = language
        self.latest_log_path, self.latest_log_excerpt = _latest_log_excerpt(self.language)
        self.persist_config(notify_user=False)
        self.notify(self.t("界面语言已切换。"), color="positive")
        ui.run_javascript("window.location.reload()")

    def assess_api_card(self, section_name: str) -> tuple[str, str]:
        card = self.api_cards[section_name]
        api_base = str(card["api_base"] or "").strip()
        model = str(card["model"] or "").strip()
        api_key = str(card["api_key"] or "").strip()

        if not api_base:
            return "warning", self.t("请先填写 API Base。")
        if not api_base.startswith(("http://", "https://")):
            return "negative", self.t("API Base 格式不正确，应以 http:// 或 https:// 开头。")
        if "/chat/completions" in api_base.rstrip("/"):
            return "warning", self.t("API Base 看起来填到了接口路径；可点击“规范化 URL”自动修正。")
        if not model:
            return "warning", self.t("请先填写模型名。")
        if not api_key:
            return "warning", self.t("API Key 还没有填写。")
        return "positive", self.t("当前配置格式看起来正确，可以点击“测试连接”。")

    def preview_api_config(self, section_name: str, *, notify_user: bool = False) -> None:
        tone, message = self.assess_api_card(section_name)
        self.show_api_feedback(section_name, message, tone=tone)
        if notify_user:
            color = {"positive": "positive", "negative": "negative", "warning": "warning", "info": "info"}.get(tone, "info")
            self.notify(message, color=color)

    async def choose_path(
        self,
        *,
        section: str,
        key: str,
        pick: str,
        title: str,
        filetypes: Iterable[tuple[str, str]] | None = None,
    ) -> None:
        current_value = str(self.state[section][key] or "")
        chosen = await self.browse_path(current_value=current_value, pick=pick, title=title, filetypes=filetypes)

        if not chosen:
            self.notify(self.t("未选择任何路径。"), color="info")
            return

        self.state[section][key] = chosen
        self.set_status(self.tf("已选择路径：{target}", target=chosen))
        self.notify(self.tf("已选择路径：{target}", target=chosen), color="positive", multi_line=True)

    async def browse_path(
        self,
        *,
        current_value: str,
        pick: str,
        title: str,
        filetypes: Iterable[tuple[str, str]] | None = None,
    ) -> str:
        if self.test_mode:
            return current_value or str(REPO_ROOT / ("output" if pick == "directory" else "config.ini"))
        else:
            return await asyncio.to_thread(
                _open_native_path_dialog,
                pick=pick,
                initial_path=current_value,
                title=title,
                filetypes=filetypes,
            )

    def run_search(self, query_override: str | None = None) -> None:
        query = str(query_override if query_override is not None else self.search_query or "").strip()
        self.search_query = query
        if not query:
            self.notify(self.t("请先输入想找的功能。"), color="warning")
            return

        normalized_query = query.lower()
        best_match: dict[str, Any] | None = None
        best_score = -1

        for item in SEARCH_ITEMS:
            terms = [item["label"], self.t(item["label"]), *item["keywords"]]
            score = 0
            for term in {str(term).strip().lower() for term in terms if str(term).strip()}:
                if normalized_query == term:
                    score = max(score, 100)
                elif normalized_query in term:
                    score = max(score, 80)
                elif term in normalized_query:
                    score = max(score, 60)
            if score > best_score:
                best_score = score
                best_match = item

        if best_match and best_score >= 60:
            destination = self.t(best_match["label"])
            self.set_status(self.tf("已跳转到 {destination}", destination=destination))
            ui.navigate.to(best_match["route"])
            self.notify(self.tf("已跳转到 {destination}", destination=destination), color="positive")
            return

        self.notify(self.tf("没有找到和 “{query}” 最接近的功能。", query=query), color="warning")

    def validate_workflow_request(self, action: str) -> bool:
        project_name = str(self.state["workflow"]["project_name"]).strip()
        pdf_folder = str(self.state["workflow"]["pdf_folder"]).strip()
        zotero_report = str(self.state["paths"]["zotero_report"]).strip()

        if not project_name:
            self.notify(self.t("请先填写项目名。"), color="warning")
            return False

        if self.free_mode_messages and not self.free_mode_profile_path:
            self.notify(self.t("自由模式对话还没有应用到本次任务。请先应用当前规划，或清空对话后再运行。"), color="warning", multi_line=True)
            return False

        if action in {"analyze", "run_all"} and not pdf_folder and not zotero_report:
            self.notify(self.t("请填写 PDF 文件夹，或先在“环境与路径”页面填写 Zotero 报告路径。"), color="warning", multi_line=True)
            return False

        if action == "generate_section":
            section_number_raw = str(self.state["workflow"]["section_number"]).strip()
            if not section_number_raw.isdigit() or int(section_number_raw) <= 0:
                self.notify(self.t("请先输入有效的章节号。"), color="warning")
                return False

        return True

    async def handle_test_api(self, section_name: str) -> None:
        if self.test_mode:
            message = self.tf("测试模式：已模拟 API 连通性检查（{section_name}）", section_name=section_name)
            self.set_status(message)
            self.show_api_feedback(section_name, message, tone="positive")
            self.notify(message, color="positive")
            return

        card = self.api_cards[section_name]
        api_base = normalize_api_base(card["api_base"], provider=card["provider"])
        card["api_base"] = api_base
        ok, message = await asyncio.to_thread(
            test_api_endpoint,
            card["api_key"],
            api_base,
            card["model"],
        )
        self.show_api_feedback(section_name, message, tone="positive" if ok else "negative")
        self.notify(f"{section_name}: {message}", color="positive" if ok else "negative", multi_line=True)

    async def run_workflow(self, action: str) -> None:
        if not self.validate_workflow_request(action):
            return

        if self.workflow_running:
            self.notify(self.t("正在刷新任务进度…"), color="info")
            return

        self.persist_config(notify_user=False)
        action_label_text = self.action_label(action)
        self.set_status(self.tf("正在执行 {action_label}，请稍候……", action_label=action_label_text))
        self.progress_tracker = ProgressTracker()
        self.progress_tracker.reset(task_type=action_label_text, stage=action, message=self.status_message, indeterminate=True)
        self.progress_snapshot = self.progress_tracker.snapshot()
        self.set_workflow_running(True)
        self.update_progress_widgets()

        if self.test_mode:
            self.progress_tracker.finish(
                success=True,
                message=self.tf("测试模式：已模拟执行 {action_label}", action_label=action_label_text),
            )
            self.progress_snapshot = self.progress_tracker.snapshot()
            self.update_progress_widgets()
            self.set_status(self.progress_snapshot.get("message", ""))
            self.set_workflow_running(False)
            self.notify(self.status_message, color="positive")
            return

        project_name = str(self.state["workflow"]["project_name"]).strip() or None
        pdf_folder = str(self.state["workflow"]["pdf_folder"]).strip() or None
        concept = str(self.state["workflow"]["concept"]).strip() or None
        free_mode_profile = self.free_mode_profile_path or None
        free_mode_idea = None
        if not free_mode_profile:
            free_mode_idea = str(self.state["workflow"]["free_mode_idea"]).strip() or None

        generate_section = None
        if action == "generate_section":
            generate_section = int(str(self.state["workflow"]["section_number"]).strip())

        try:
            result = await asyncio.to_thread(
                run_dispatch,
                build_args(
                    config=self.config_path,
                    project_name=project_name,
                    pdf_folder=pdf_folder,
                    concept=concept,
                    free_mode_profile=free_mode_profile,
                    free_mode_idea=free_mode_idea,
                    progress_tracker=self.progress_tracker,
                    run_all=action == "run_all",
                    analyze_only=action == "analyze",
                    generate_outline=action == "outline",
                    generate_review=action == "review",
                    generate_section=generate_section,
                    validate_review=action == "validate",
                    retry_failed=action == "retry_failed",
                    retry_review_failed=action == "retry_review_failed",
                ),
            )
        finally:
            self.refresh_progress()
            self.set_workflow_running(False)
        self.refresh_logs()
        if result.success:
            self.set_status(self.tf("{action_label} 已执行完成。", action_label=action_label_text))
            self.notify(self.status_message, color="positive")
        else:
            self.set_status(
                self.tf(
                    "{action_label} 执行失败：{reason}",
                    action_label=action_label_text,
                    reason=result.message or result.exit_code,
                )
            )
            self.notify(self.status_message, color="negative", multi_line=True)


def _render_progress_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
        ui.label(t("任务进度")).classes("ag-section-title")
        ui.label(t("总体进度")).classes("ag-subtle q-mt-sm")
        overall_bar = ui.linear_progress(value=0).classes("w-full q-mt-md")
        ui.label(t("阶段进度")).classes("ag-subtle q-mt-sm")
        stage_bar = ui.linear_progress(value=0).classes("w-full q-mt-sm")
        with ui.grid(columns=2).classes("w-full gap-3 q-mt-md"):
            with ui.column().classes("gap-1"):
                ui.label(t("当前任务")).classes("ag-subtle")
                task_label = ui.label(t("暂无运行中的任务")).classes("text-body1")
            with ui.column().classes("gap-1"):
                ui.label(t("当前阶段")).classes("ag-subtle")
                stage_label = ui.label("-").classes("text-body1")
            with ui.column().classes("gap-1"):
                ui.label(t("当前对象")).classes("ag-subtle")
                item_label = ui.label("-").classes("text-body1")
            with ui.column().classes("gap-1"):
                ui.label(t("成功 / 失败 / 剩余")).classes("ag-subtle")
                counts_label = ui.label("0 / 0 / 0").classes("text-body1")
            with ui.column().classes("gap-1"):
                ui.label(t("重试轮次")).classes("ag-subtle")
                retry_label = ui.label("-").classes("text-body1")
            with ui.column().classes("gap-1"):
                ui.label(t("已耗时")).classes("ag-subtle")
                elapsed_label = ui.label("00:00").classes("text-body1")
        ui.label(t("等待模型返回或执行长任务中…")).classes("ag-subtle q-mt-md")
        message_label = ui.label(t("暂无运行中的任务")).classes("text-body2")
        controller.register_progress_widgets(
            task_label=task_label,
            stage_label=stage_label,
            message_label=message_label,
            item_label=item_label,
            counts_label=counts_label,
            retry_label=retry_label,
            elapsed_label=elapsed_label,
            overall_bar=overall_bar,
            stage_bar=stage_bar,
        )


def _render_workflow_input_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
        ui.label(t("任务输入")).classes("ag-section-title")
        ui.label(t("先确定这次任务从 PDF 文件夹还是 Zotero 报告开始，再补充项目名。")).classes("ag-subtle")
        with ui.grid(columns=2).classes("w-full gap-3 q-mt-md"):
            ui.input(t("项目名"), value=controller.state["workflow"]["project_name"]).bind_value(controller.state["workflow"], "project_name")
            _render_path_field(
                controller,
                label="PDF 文件夹",
                section="workflow",
                key="pdf_folder",
                pick="directory",
                title="选择 PDF 文件夹",
            )
            _render_path_field(
                controller,
                label="Zotero 报告路径",
                section="paths",
                key="zotero_report",
                pick="file",
                title="选择 Zotero 报告文件",
                filetypes=[("Report Files", "*.html *.htm *.txt *.md *.csv *.json"), ("All Files", "*.*")],
            )
        with ui.row().classes("gap-2 q-mt-sm flex-wrap"):
            ui.button(t("打开 PDF 文件夹"), on_click=lambda: _open_path(controller.state["workflow"]["pdf_folder"], controller.language)).props("outline")
            ui.button(t("打开 Zotero 报告"), on_click=lambda: _open_path(controller.state["paths"]["zotero_report"], controller.language)).props("outline")
            ui.button(t("前往 Setup"), on_click=lambda: ui.navigate.to("/setup")).props("outline")


def _render_workflow_concept_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full"):
        ui.label(t("概念增强模式")).classes("ag-section-title")
        ui.label(t("如果这次要围绕某个核心概念补抓变量、定义和比较关系，就填写概念词。普通模式可以留空。")).classes("ag-subtle")
        ui.input(
            t("概念增强模式概念词"),
            value=controller.state["workflow"]["concept"],
        ).bind_value(controller.state["workflow"], "concept").classes("w-full q-mt-md")


def _render_free_mode_planner_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
        ui.label(t("自由模式对话规划器")).classes("ag-section-title")
        ui.label(t("先和规划助手多轮聊清楚你的综述想法，再把当前规划应用到本次任务。")).classes("ag-subtle")
        status_label = ui.label("").classes("text-body1 q-mt-sm")
        hint_label = ui.label("").classes("ag-subtle")
        with ui.grid(columns=2).classes("w-full gap-4 q-mt-md"):
            transcript_view = ui.textarea(
                label=t("对话记录"),
                value="",
            ).props("outlined readonly autogrow").classes("w-full")
            profile_view = ui.textarea(
                label=t("当前 profile 草案"),
                value="",
            ).props("outlined readonly autogrow").classes("w-full")
        planner_input = ui.textarea(
            label=t("继续告诉规划助手"),
            value=controller.free_mode_chat_input,
            placeholder=t("例如：文件夹里主要有概念 A 和 B，我想写 A 如何推导到 B，重点比较理论解释、变量链路和 research gap。"),
        ).bind_value(controller, "free_mode_chat_input").props("outlined autogrow").classes("w-full q-mt-md")
        with ui.row().classes("gap-2 q-mt-sm flex-wrap"):
            send_button = ui.button(t("发送给规划助手"), on_click=lambda: asyncio.create_task(controller.send_free_mode_message())).props("unelevated")
            apply_button = ui.button(t("应用到本次任务"), on_click=lambda: asyncio.create_task(controller.apply_free_mode_plan())).props("outline")
            reset_button = ui.button(t("清空自由模式对话"), on_click=controller.clear_free_mode_planner).props("outline")
        controller.register_free_mode_widgets(
            transcript_view=transcript_view,
            profile_view=profile_view,
            status_label=status_label,
            hint_label=hint_label,
            send_button=send_button,
            apply_button=apply_button,
            reset_button=reset_button,
        )


def _render_workflow_actions_card(controller: WorkspaceController) -> None:
    t = controller.t
    action_specs = [
        ("仅分析文献", "先检查文献提取、预处理和结构化结果是否稳定。", "analyze"),
        ("生成大纲", "在分析结果基础上先搭出综述结构。", "outline"),
        ("生成全文", "直接生成正文，适合已经确认过结构和素材的任务。", "review"),
        ("一键运行", "从分析到正文一口气跑完，适合稳定的批量流程。", "run_all"),
    ]

    with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
        ui.label(t("主流程操作")).classes("ag-section-title")
        ui.label(t("把主流程按钮集中放在一起，只保留真正代表分析链路的四个入口。")).classes("ag-subtle")
        with ui.element("div").classes("ag-action-grid w-full q-mt-md"):
            for label_key, desc_key, action in action_specs:
                with ui.element("div").classes("ag-action-tile"):
                    ui.label(t(label_key)).classes("ag-section-title")
                    ui.label(t(desc_key)).classes("ag-subtle")
                    button = ui.button(
                        t(label_key),
                        on_click=lambda event=None, current_action=action: asyncio.create_task(controller.run_workflow(current_action)),
                    ).props("unelevated").classes("w-full")
                    controller.register_action_button(button)


def _render_workflow_recovery_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full"):
        ui.label(t("补跑与质检")).classes("ag-section-title")
        ui.label(t("把修复、补跑和验证入口单独放在这里，避免和首次运行按钮混在一起。")).classes("ag-subtle")
        with ui.element("div").classes("ag-mini-grid w-full q-mt-md"):
            with ui.element("div").classes("ag-mini-card"):
                ui.label(t("章节操作")).classes("ag-section-title")
                ui.label(t("如果某一章中途失败，可以单独补写，或者只补跑失败章节。")).classes("ag-subtle")
                ui.input(t("章节号"), value=controller.state["workflow"]["section_number"]).bind_value(controller.state["workflow"], "section_number").classes("w-full q-mt-sm")
                with ui.column().classes("ag-button-column q-mt-md"):
                    section_button = ui.button(
                        t("补写指定章节"),
                        on_click=lambda: asyncio.create_task(controller.run_workflow("generate_section")),
                    ).props("unelevated").classes("w-full")
                    retry_review_button = ui.button(
                        t("重试失败章节"),
                        on_click=lambda: asyncio.create_task(controller.run_workflow("retry_review_failed")),
                    ).props("outline").classes("w-full")
                controller.register_action_button(section_button)
                controller.register_action_button(retry_review_button)

            with ui.element("div").classes("ag-mini-card"):
                ui.label(t("失败论文")).classes("ag-section-title")
                ui.label(t("如果只有个别论文失败，可以单独补跑，不影响已经完成的结果。")).classes("ag-subtle")
                retry_failed_button = ui.button(
                    t("重试失败论文"),
                    on_click=lambda: asyncio.create_task(controller.run_workflow("retry_failed")),
                ).props("outline").classes("w-full q-mt-md")
                controller.register_action_button(retry_failed_button)

            with ui.element("div").classes("ag-mini-card"):
                ui.label(t("质量检查")).classes("ag-section-title")
                ui.label(t("验证功能默认关闭，暂时作为实验功能保留。")).classes("ag-subtle")
                validate_button = ui.button(
                    t("验证综述"),
                    on_click=lambda: asyncio.create_task(controller.run_workflow("validate")),
                ).props("outline").classes("w-full q-mt-md")
                controller.register_action_button(validate_button)


def _render_workflow_checklist_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full"):
        ui.label(t("开始前快速检查")).classes("ag-section-title")
        ui.label(t("把准备项单独放到侧边后，这里只保留真正会影响流程成败的检查。")).classes("ag-subtle")
        with ui.column().classes("ag-checklist q-mt-md"):
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("先确认输出目录和 Zotero / PDF 路径可用。")).classes("ag-subtle")
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("再检查阅读模型、写作模型、大纲模型和自由模式对话模型都已经连通。")).classes("ag-subtle")
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("如果这批 PDF 质量参差不齐，建议先开预处理和 OCR 自动模式。")).classes("ag-subtle")
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("最后决定是先跑分析，还是直接一键运行。")).classes("ag-subtle")


def _render_workflow_navigation_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full"):
        ui.label(t("工作台导航")).classes("ag-section-title")
        ui.label(t("配置、日志和目录入口单独收纳在这里，不再和运行按钮放在同一组。")).classes("ag-subtle")
        with ui.column().classes("ag-button-column q-mt-md"):
            ui.button(t("前往 Setup"), on_click=lambda: ui.navigate.to("/setup")).props("outline").classes("w-full")
            ui.button(t("打开 API 页面"), on_click=lambda: ui.navigate.to("/setup/api")).props("outline").classes("w-full")
            ui.button(t("查看日志与产物"), on_click=lambda: ui.navigate.to("/logs")).props("outline").classes("w-full")
            ui.button(t("刷新日志"), on_click=controller.refresh_logs).props("outline").classes("w-full")
            ui.button(t("打开输出目录"), on_click=lambda: _open_path(controller.state["paths"]["output_path"], controller.language)).props("outline").classes("w-full")
            ui.button(t("打开日志目录"), on_click=lambda: _open_path(str(REPO_ROOT / "logs"), controller.language)).props("outline").classes("w-full")


def _render_path_field(
    controller: WorkspaceController,
    *,
    label: str,
    section: str,
    key: str,
    pick: str,
    title: str,
    filetypes: Iterable[tuple[str, str]] | None = None,
) -> None:
    value_binding = controller.state[section]
    with ui.column().classes("w-full gap-2"):
        display_input = ui.input(controller.t(label), value=value_binding[key]).bind_value(value_binding, key).props("readonly").classes("w-full")
        with ui.dialog() as dialog, ui.card().classes("ag-card p-5 min-w-[560px] max-w-[760px] w-full"):
            ui.label(f"{controller.t('配置路径')} · {controller.t(label)}").classes("ag-section-title")
            ui.label(controller.t("手动输入或用选择按钮更新路径。")).classes("ag-subtle")
            temp_state = {"value": str(value_binding[key] or "")}
            edit_input = ui.input(controller.t(label), value=temp_state["value"]).bind_value(temp_state, "value").classes("w-full")

            async def choose_for_dialog() -> None:
                chosen = await controller.browse_path(
                    current_value=str(temp_state["value"] or ""),
                    pick=pick,
                    title=controller.t(title),
                    filetypes=filetypes,
                )
                if chosen:
                    temp_state["value"] = chosen
                    edit_input.value = chosen

            def save_dialog_value() -> None:
                value_binding[key] = str(temp_state["value"] or "")
                display_input.value = value_binding[key]
                dialog.close()

            with ui.row().classes("gap-2 q-mt-md"):
                ui.button(controller.t("浏览并选择"), on_click=lambda: asyncio.create_task(choose_for_dialog())).props("outline")
                ui.button(controller.t("取消"), on_click=dialog.close).props("flat")
                ui.button(controller.t("保存路径设置"), on_click=save_dialog_value).props("unelevated")

        ui.button(controller.t(title), on_click=dialog.open).props("outline size=sm")


def _nav_groups() -> Iterable[Dict[str, Any]]:
    return NAV_GROUPS


@contextmanager
def _page_shell(controller: WorkspaceController, page_title: str, subtitle: str, active_route: str):
    ui.colors(primary="#5b6d66", secondary="#dde5df", accent="#8ea097")
    controller.register_client(ui.context.client)
    with ui.left_drawer(top_corner=True, bottom_corner=True).classes("ag-drawer").props("bordered"):
        ui.label("Auto Generate").classes("ag-chip q-mb-sm")
        ui.label(controller.t("目录")).classes("ag-section-title q-mb-md")
        for group in _nav_groups():
            with ui.column().classes("ag-nav-group w-full"):
                ui.label(controller.t(group["title"])).classes("ag-nav-title")
                for label, route, description, icon_name in group["items"]:
                    classes = "ag-nav-link ag-nav-link-active" if route == active_route else "ag-nav-link"
                    with ui.link(target=route).classes(classes):
                        with ui.row().classes("items-start no-wrap gap-3 w-full"):
                            ui.icon(icon_name).classes("text-lg")
                            with ui.column().classes("gap-0.5"):
                                ui.label(controller.t(label)).classes("ag-nav-label")
                                ui.label(controller.t(description)).classes("ag-nav-desc")
    with ui.element("div").classes("ag-fixedbar"):
        with ui.element("div").classes("ag-fixedbar-shell"):
            with ui.element("div").classes("ag-fixedbar-main"):
                with ui.element("div").classes("ag-title-stack"):
                    ui.label(controller.t("AI 文献综述生成器")).classes("ag-section-title ag-topbar-title")
                    ui.label(f"Build {BUILD_STAMP}").classes("ag-build-badge")
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    search_input = ui.input(
                        controller.t("搜索功能"),
                        placeholder=controller.t("例如：大纲、OCR、日志、自由模式"),
                    ).classes("ag-search")
                    search_input.bind_value(controller, "search_query")
                    search_input.props("outlined clearable")
                    search_input.on("keydown.enter", lambda _: controller.run_search(search_input.value))
                    ui.button(
                        controller.t("搜索"),
                        icon="search",
                        on_click=lambda: controller.run_search(search_input.value),
                    ).classes("ag-search-button").props("unelevated")
                with ui.row().classes("ag-fixedbar-tools"):
                    ui.select(
                        LANGUAGE_OPTIONS,
                        value=controller.language,
                        label=controller.t("语言"),
                        on_change=lambda event: controller.change_language(str(event.value)),
                    ).classes("min-w-[150px]")
                    ui.button(controller.t("保存配置"), on_click=lambda: controller.persist_config()).props("unelevated")
                    ui.button(controller.t("核心工作台"), on_click=lambda: ui.navigate.to("/workflow")).props("outline")
                    ui.button(controller.t("打开输出"), on_click=lambda: _open_path(controller.state["paths"]["output_path"], controller.language)).props("outline")
                    ui.button(controller.t("打开日志"), on_click=lambda: _open_path(str(REPO_ROOT / "logs"), controller.language)).props("outline")
    with ui.column().classes("ag-page w-full gap-5"):
        with ui.element("div").classes("ag-reminder ag-page-reminder"):
            ui.icon("tips_and_updates").classes("text-lg")
            status_label = ui.label("").classes("ag-reminder-text")
            controller.register_status_label(status_label)
        with ui.column().classes("ag-page-head"):
            ui.label(controller.t(page_title)).classes("ag-page-title")
            ui.label(controller.t(subtitle)).classes("ag-page-subtitle")
        yield


def _render_api_card(controller: WorkspaceController, section_name: str, title: str, description: str) -> None:
    card = controller.api_cards[section_name]
    with ui.card().classes("ag-card p-5 w-full"):
        ui.label(controller.t(title)).classes("ag-section-title")
        ui.label(controller.t(description)).classes("ag-subtle")
        with ui.row().classes("ag-inline-alert ag-inline-alert-info w-full hidden") as feedback_box:
            feedback_icon = ui.icon("info").classes("text-base")
            feedback_label = ui.label("").classes("ag-subtle")
            ui.space()
            ui.button(icon="close", on_click=lambda _event, s=section_name: controller.hide_api_feedback(s)).props("flat round dense")
        controller.register_api_feedback(section_name, feedback_box, feedback_label, feedback_icon)
        with ui.grid(columns=2).classes("w-full gap-3"):
            provider_select = ui.select(
                {key: preset.label for key, preset in PROVIDER_PRESETS.items()},
                value=card["provider"],
                label=controller.t("服务商"),
            )
            provider_select.bind_value(card, "provider")
            model_input = ui.input(controller.t("模型名"), value=card["model"]).bind_value(card, "model")
            api_base_input = ui.input("API Base", value=card["api_base"]).bind_value(card, "api_base")
            api_key_input = ui.input("API Key", value=card["api_key"], password=True, password_toggle_button=True).bind_value(card, "api_key")

        provider_select.on("update:model-value", lambda _: controller.preview_api_config(section_name))
        model_input.on("blur", lambda _: controller.preview_api_config(section_name))
        model_input.on("update:model-value", lambda _: controller.preview_api_config(section_name))
        api_base_input.on("blur", lambda _: controller.preview_api_config(section_name))
        api_base_input.on("update:model-value", lambda _: controller.preview_api_config(section_name))
        api_key_input.on("blur", lambda _: controller.preview_api_config(section_name))
        api_key_input.on("update:model-value", lambda _: controller.preview_api_config(section_name))

        def apply_preset() -> None:
            provider = provider_select.value or "custom"
            preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])
            card["provider"] = provider
            card["api_base"] = preset.default_api_base
            api_base_input.value = preset.default_api_base
            controller.preview_api_config(section_name, notify_user=True)

        def normalize_base() -> None:
            provider = provider_select.value or "custom"
            normalized = normalize_api_base(api_base_input.value or "", provider=provider)
            card["provider"] = provider
            card["api_base"] = normalized
            api_base_input.value = normalized
            controller.preview_api_config(section_name, notify_user=True)

        with ui.row().classes("gap-2 q-mt-sm"):
            ui.button(controller.t("套用预设 URL"), on_click=apply_preset).props("outline")
            ui.button(controller.t("规范化 URL"), on_click=normalize_base).props("outline")
            ui.button(controller.t("检查配置"), on_click=lambda _event, s=section_name: controller.preview_api_config(s, notify_user=True)).props("outline")
            ui.button(
                controller.t("测试连接"),
                on_click=lambda _event, s=section_name: asyncio.create_task(controller.handle_test_api(s)),
            ).props("outline")


def _render_environment_card(controller: WorkspaceController) -> None:
    copy = _environment_ui_copy(controller.language, controller.runtime_environment)
    with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
        ui.label(copy["title"]).classes("ag-section-title")
        ui.label(copy["intro"]).classes("ag-subtle")
        with ui.element("div").classes("ag-mini-grid q-mt-md"):
            with ui.element("div").classes("ag-mini-card"):
                ui.label(copy["env_type_label"]).classes("ag-subtle")
                ui.label(copy["env_type"]).classes("ag-section-title")
            with ui.element("div").classes("ag-mini-card"):
                ui.label(copy["env_name_label"]).classes("ag-subtle")
                ui.label(copy["env_name"]).classes("ag-section-title")
            with ui.element("div").classes("ag-mini-card"):
                ui.label(copy["isolation_label"]).classes("ag-subtle")
                ui.label(copy["isolation"]).classes("ag-section-title")
        ui.label(copy["recommendation"]).classes("ag-subtle q-mt-md")
        ui.label(f'{copy["executable_label"]}:').classes("ag-subtle q-mt-sm")
        ui.label(copy["executable"]).style("font-family: Consolas, 'Courier New', monospace; word-break: break-all;")
        if controller.runtime_environment.needs_isolation_recommendation:
            ui.label(copy["command_title"]).classes("ag-subtle q-mt-md")
            ui.label(copy["create_command"]).style("font-family: Consolas, 'Courier New', monospace; word-break: break-all;")
            ui.label(copy["activate_command"]).style("font-family: Consolas, 'Courier New', monospace; word-break: break-all;")


def launch_gui(
    config_path: str = "config.ini",
    port: int = DEFAULT_PORT,
    *,
    reload: bool = False,
    show: bool = True,
) -> None:
    config_path = str((REPO_ROOT / config_path).resolve()) if not os.path.isabs(config_path) else config_path
    controller = WorkspaceController(config_path=config_path)

    ui.add_head_html(STYLE_BLOCK, shared=True)

    @ui.page("/")
    def dashboard_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "总览",
            "这个入口页负责给第一次使用的人建立清晰路径。真正的核心操作已经单独放到“核心工作台”，不再堆在页面最底部。",
            "/",
        ):
            with ui.card().classes("ag-card ag-card-strong ag-hero p-7 w-full"):
                with ui.grid(columns=2).classes("w-full items-stretch gap-6"):
                    with ui.column().classes("ag-card-stack"):
                        ui.label(t("本地网页工作台")).classes("ag-chip")
                        ui.label(t("适合自己用，也适合交给第一次接触项目的人。")).classes("ag-section-title")
                        ui.label(
                            t("推荐顺序是：先完成 setup 和 API 连接，再去核心工作台填写项目名、PDF 文件夹或 Zotero 配置，最后运行分析、大纲或全文写作。"),
                        ).classes("ag-subtle")
                        ui.label(t("这页先帮你把入口、模式和准备项理顺，再开始正式跑任务。")).classes("ag-subtle")
                        with ui.row().classes("ag-card-actions"):
                            ui.button(t("进入核心工作台"), on_click=lambda: ui.navigate.to("/workflow")).props("unelevated")
                            ui.button(t("先做 setup"), on_click=lambda: ui.navigate.to("/setup")).props("outline")
                            ui.button(t("打开 API 页面"), on_click=lambda: ui.navigate.to("/setup/api")).props("outline")
                    with ui.column().classes("ag-card-stack"):
                        with ui.card().classes("ag-card p-4"):
                            ui.label(t("当前输出目录")).classes("ag-subtle")
                            ui.label(controller.state["paths"]["output_path"] or "./output").classes("text-body1")
                        with ui.card().classes("ag-card p-4"):
                            ui.label(t("最近日志数量")).classes("ag-subtle")
                            ui.label(str(_count_log_files())).classes("ag-kpi")
                        with ui.card().classes("ag-card p-4"):
                            ui.label(t("预处理状态")).classes("ag-subtle")
                            ui.label(t("已启用") if controller.state["preprocess"]["enabled"] else t("未启用")).classes("ag-kpi")

            with ui.element("div").classes("ag-grid-3 w-full"):
                with ui.card().classes("ag-card p-5 h-full"):
                    with ui.column().classes("ag-card-stack"):
                        ui.label(t("1. 环境与路径")).classes("ag-section-title")
                        ui.label(t("在 GUI 内完成 setup、路径填写和输出目录配置。")).classes("ag-subtle")
                        with ui.row().classes("ag-card-actions"):
                            ui.button(t("打开环境与路径"), on_click=lambda: ui.navigate.to("/setup")).props("outline")
                with ui.card().classes("ag-card p-5 h-full"):
                    with ui.column().classes("ag-card-stack"):
                        ui.label(t("2. API 与模型")).classes("ag-section-title")
                        ui.label(t("阅读、写作、框架大纲和验证模型都可分别配置，并支持连通性测试。")).classes("ag-subtle")
                        with ui.row().classes("ag-card-actions"):
                            ui.button(t("打开 API 页面"), on_click=lambda: ui.navigate.to("/setup/api")).props("outline")
                with ui.card().classes("ag-card p-5 h-full"):
                    with ui.column().classes("ag-card-stack"):
                        ui.label(t("3. 核心工作台")).classes("ag-section-title")
                        ui.label(t("项目名、自由模式、分析、大纲和一键运行都集中在这里。")).classes("ag-subtle")
                        with ui.row().classes("ag-card-actions"):
                            ui.button(t("开始运行"), on_click=lambda: ui.navigate.to("/workflow")).props("outline")

            with ui.element("div").classes("ag-grid-2 w-full"):
                with ui.card().classes("ag-card p-6 h-full"):
                    with ui.column().classes("ag-card-stack"):
                        ui.label(t("三种使用方式")).classes("ag-section-title")
                        ui.label(t("不用先记命令。先选路径，再选模式，再决定是先分析还是直接一键运行。")).classes("ag-subtle")
                        with ui.element("div").classes("ag-mini-grid"):
                            with ui.element("div").classes("ag-mini-card"):
                                ui.label(t("普通模式")).classes("ag-section-title")
                                ui.label(t("普通模式适合先批量读文献，再统一生成大纲和正文。")).classes("ag-subtle")
                            with ui.element("div").classes("ag-mini-card"):
                                ui.label(t("概念增强模式")).classes("ag-section-title")
                                ui.label(t("概念增强模式适合围绕某个核心概念补抓变量、定义与比较。")).classes("ag-subtle")
                            with ui.element("div").classes("ag-mini-card"):
                                ui.label(t("自由模式")).classes("ag-section-title")
                                ui.label(t("自由模式适合先把你的研究意图聊清楚，再转成 prompt profile。")).classes("ag-subtle")
                with ui.card().classes("ag-card p-6 h-full"):
                    with ui.column().classes("ag-card-stack"):
                        ui.label(t("开始前快速检查")).classes("ag-section-title")
                        ui.label(t("先把这四件事看一遍，能减少很多中途报错和重复返工。")).classes("ag-subtle")
                        with ui.column().classes("ag-checklist"):
                            with ui.element("div").classes("ag-check-item"):
                                ui.icon("check_circle").classes("text-base")
                                ui.label(t("先确认输出目录和 Zotero / PDF 路径可用。")).classes("ag-subtle")
                            with ui.element("div").classes("ag-check-item"):
                                ui.icon("check_circle").classes("text-base")
                                ui.label(t("再检查阅读模型、写作模型、大纲模型和自由模式对话模型都已经连通。")).classes("ag-subtle")
                            with ui.element("div").classes("ag-check-item"):
                                ui.icon("check_circle").classes("text-base")
                                ui.label(t("如果这批 PDF 质量参差不齐，建议先开预处理和 OCR 自动模式。")).classes("ag-subtle")
                            with ui.element("div").classes("ag-check-item"):
                                ui.icon("check_circle").classes("text-base")
                                ui.label(t("最后回到核心工作台，先跑分析，再决定是否一键运行。")).classes("ag-subtle")
                        with ui.row().classes("ag-card-actions"):
                            ui.button(t("打开环境与路径"), on_click=lambda: ui.navigate.to("/setup")).props("outline")
                            ui.button(t("打开 API 页面"), on_click=lambda: ui.navigate.to("/setup/api")).props("outline")
                            ui.button(t("进入核心工作台"), on_click=lambda: ui.navigate.to("/workflow")).props("unelevated")

    @ui.page("/workflow")
    def workflow_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "核心工作台",
            "把真正的工作区单独抽出来放在前面。你在这里输入项目、选择模式并直接运行，不需要翻很长的页面。",
            "/workflow",
        ):
            with ui.element("div").classes("ag-workflow-shell w-full"):
                with ui.column().classes("gap-5 w-full"):
                    _render_workflow_input_card(controller)
                    _render_workflow_concept_card(controller)
                    _render_free_mode_planner_card(controller)
                    _render_workflow_actions_card(controller)
                    _render_workflow_recovery_card(controller)
                with ui.column().classes("ag-sidebar-stack w-full"):
                    _render_progress_card(controller)
                    _render_workflow_checklist_card(controller)
                    _render_workflow_navigation_card(controller)
            ui.timer(1.0, controller.refresh_progress)

    @ui.page("/setup")
    def setup_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "环境与路径",
            "这里替代原来的命令行 setup。第一次使用的人只需要顺着页面填写，不需要进命令行敲配置。",
            "/setup",
        ):
            _render_environment_card(controller)
            with ui.element("div").classes("ag-grid-2 w-full"):
                with ui.card().classes("ag-card ag-card-strong p-6"):
                    ui.label(t("基础路径")).classes("ag-section-title")
                    ui.label(t("如果你主要用 PDF 文件夹模式，可以只填输出目录；如果你用 Zotero 报告模式，再补 Zotero 相关路径。")).classes("ag-subtle")
                    with ui.column().classes("w-full gap-3 q-mt-md"):
                        ui.input(t("config.ini 路径"), value=controller.config_path).props("readonly")
                        _render_path_field(
                            controller,
                            label="输出目录",
                            section="paths",
                            key="output_path",
                            pick="directory",
                            title="选择输出目录",
                        )
                        _render_path_field(
                            controller,
                            label="Zotero 报告路径",
                            section="paths",
                            key="zotero_report",
                            pick="file",
                            title="选择 Zotero 报告文件",
                            filetypes=[("Report Files", "*.html *.htm *.txt *.md *.csv *.json"), ("All Files", "*.*")],
                        )
                        _render_path_field(
                            controller,
                            label="Zotero 库路径",
                            section="paths",
                            key="library_path",
                            pick="directory",
                            title="选择 Zotero 库目录",
                        )
                    with ui.row().classes("gap-2 q-mt-sm"):
                        ui.button(t("保存配置"), on_click=lambda: controller.persist_config()).props("unelevated")
                        ui.button(t("打开输出目录"), on_click=lambda: _open_path(controller.state["paths"]["output_path"], controller.language)).props("outline")
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("首次使用建议")).classes("ag-section-title")
                    ui.label(t("1. 先在这里填好输出目录和 Zotero 相关路径。")).classes("ag-subtle")
                    ui.label(t("2. 再去“API 与模型”页补模型、API Base 和 API Key。")).classes("ag-subtle")
                    ui.label(t("3. 如果 API Base 填错格式，保存时会自动规范化。")).classes("ag-subtle")
                    ui.label(t("4. 配置保存后，API Key 会写入 `.env`，不用手改文本文件。")).classes("ag-subtle")
                    with ui.row().classes("gap-2 q-mt-md"):
                        ui.button(t("前往 API 与模型"), on_click=lambda: ui.navigate.to("/setup/api")).props("outline")
                        ui.button(t("前往性能与预处理"), on_click=lambda: ui.navigate.to("/setup/processing")).props("outline")

    @ui.page("/setup/api")
    def api_page() -> None:
        with _page_shell(
            controller,
            "API 与模型",
            "阅读模型、写作模型、大纲模型、自由模式对话模型和验证模型都在这里分开配置。每块都支持 URL 预设、自动规范化和连通性测试。",
            "/setup/api",
        ):
            with ui.column().classes("w-full gap-4"):
                _render_api_card(controller, "Primary_Reader_API", "阅读模型", "优先负责文献分析与阶段一抽取。")
                _render_api_card(controller, "Backup_Reader_API", "备用阅读模型", "当主阅读模型失败或限流时，系统可以兜底。")
                _render_api_card(controller, "Writer_API", "写作模型", "负责大段综述写作与章节生成。")
                _render_api_card(controller, "Outline_API", "大纲模型", "优先负责框架大纲规划；未配置时可回退到写作模型。")
                _render_api_card(controller, "Free_Mode_API", "自由模式对话模型", "优先负责自由模式前置对话规划；未配置时可回退到大纲模型。")
                _render_api_card(controller, "Validator_API", "验证模型", "用于综述校验和质量复查。")

    @ui.page("/setup/processing")
    def processing_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "性能与预处理",
            "这一页专门控制并发、验证、PDF 预处理、OCR 和本地 RAG。这样 setup 页面不会显得过于拥挤。",
            "/setup/processing",
        ):
            with ui.element("div").classes("ag-grid-compact w-full"):
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("运行参数")).classes("ag-section-title")
                    with ui.grid(columns=2).classes("w-full gap-3 q-mt-md"):
                        ui.input(t("最大并发"), value=controller.state["performance"]["max_workers"]).bind_value(controller.state["performance"], "max_workers")
                        ui.input(t("API 重试次数"), value=controller.state["performance"]["api_retry_attempts"]).bind_value(controller.state["performance"], "api_retry_attempts")

                with ui.card().classes("ag-card p-6"):
                    ui.label(t("阶段二重试")).classes("ag-section-title")
                    ui.label(t("阶段二自动重试会在全文生成时自动补跑失败章节。")).classes("ag-subtle")
                    with ui.grid(columns=2).classes("w-full gap-3 q-mt-md"):
                        ui.switch(t("启用阶段二自动重试"), value=controller.state["stage2_retry"]["enabled"]).bind_value(controller.state["stage2_retry"], "enabled")
                        ui.input(t("阶段二最大重试轮数"), value=controller.state["stage2_retry"]["max_retry_rounds"]).bind_value(controller.state["stage2_retry"], "max_retry_rounds")
                        ui.input(t("阶段二基础等待秒数"), value=controller.state["stage2_retry"]["base_retry_delay"]).bind_value(controller.state["stage2_retry"], "base_retry_delay")
                        ui.input(t("阶段二最大等待秒数"), value=controller.state["stage2_retry"]["max_retry_delay"]).bind_value(controller.state["stage2_retry"], "max_retry_delay")

                with ui.card().classes("ag-card ag-card-strong p-6"):
                    ui.label(t("PDF 预处理")).classes("ag-section-title")
                    ui.label(t("默认会先做缓存和诊断，再交给 AI 分析，减少直接啃 PDF 时的不稳定。")).classes("ag-subtle")
                    with ui.grid(columns=2).classes("w-full gap-3 q-mt-md"):
                        ui.switch(t("启用预处理"), value=controller.state["preprocess"]["enabled"]).bind_value(controller.state["preprocess"], "enabled")
                        ui.switch(t("强制重建缓存"), value=controller.state["preprocess"]["force_rebuild"]).bind_value(controller.state["preprocess"], "force_rebuild")
                        _render_path_field(
                            controller,
                            label="缓存目录",
                            section="preprocess",
                            key="cache_dir",
                            pick="directory",
                            title="选择缓存目录",
                        )
                        ui.input(t("OCR 语言"), value=controller.state["preprocess"]["ocr_languages"]).bind_value(controller.state["preprocess"], "ocr_languages")
                        ui.select(["auto", "fitz", "pymupdf4llm"], value=controller.state["preprocess"]["extractor_profile"], label=t("提取策略")).bind_value(controller.state["preprocess"], "extractor_profile")
                        ui.select(["auto", "off", "always"], value=controller.state["preprocess"]["ocr_mode"], label=t("OCR 模式")).bind_value(controller.state["preprocess"], "ocr_mode")
                        ui.switch(t("启用本地 RAG"), value=controller.state["preprocess"]["enable_local_rag"]).bind_value(controller.state["preprocess"], "enable_local_rag")
                        ui.select(["chroma"], value=controller.state["preprocess"]["rag_backend"], label=t("RAG 后端")).bind_value(controller.state["preprocess"], "rag_backend")
                    ui.label(t("OCR 默认只在疑似扫描页、无文本页或提取质量过低时触发，不会一上来就全量 OCR。")).classes("ag-subtle q-mt-md")

                with ui.card().classes("ag-card p-6"):
                    ui.label(t("高级 / 实验功能")).classes("ag-section-title")
                    ui.label(t("验证功能默认关闭，暂时作为实验功能保留。")).classes("ag-subtle")
                    with ui.expansion(t("高级 / 实验功能"), icon="science").classes("w-full q-mt-md"):
                        with ui.column().classes("gap-2 q-pa-sm"):
                            ui.switch(t("启用阶段一验证"), value=controller.state["performance"]["enable_stage1_validation"]).bind_value(controller.state["performance"], "enable_stage1_validation")
                            ui.switch(t("启用阶段二验证"), value=controller.state["performance"]["enable_stage2_validation"]).bind_value(controller.state["performance"], "enable_stage2_validation")

    @ui.page("/logs")
    def logs_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "日志与产物",
            "这里集中放状态、日志和目录入口。运行任务后你可以直接在这里看最近进展，而不需要回命令行。",
            "/logs",
        ):
            _render_progress_card(controller)
            with ui.element("div").classes("ag-grid-2 w-full"):
                with ui.card().classes("ag-card ag-card-strong p-6"):
                    ui.label(t("当前状态")).classes("ag-section-title")
                    status_label = ui.label("").classes("text-body1 q-mt-sm")
                    controller.register_status_label(status_label)
                    with ui.row().classes("gap-2 q-mt-md"):
                        ui.button(t("刷新日志"), on_click=controller.refresh_logs).props("unelevated")
                        ui.button(t("打开日志目录"), on_click=lambda: _open_path(str(REPO_ROOT / "logs"), controller.language)).props("outline")
                        ui.button(t("打开输出目录"), on_click=lambda: _open_path(controller.state["paths"]["output_path"], controller.language)).props("outline")
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("最近日志文件")).classes("ag-section-title")
                    log_path_label = ui.label("").classes("ag-subtle")
                    log_view = ui.textarea(value="").props("outlined readonly autogrow").classes("w-full q-mt-sm")
                    controller.register_log_widgets(log_path_label, log_view)
            ui.timer(1.0, controller.refresh_progress)

    @ui.page("/guide")
    def guide_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "使用引导",
            "这页是面向第一次使用者的说明，尽量把理解成本降下来。后面如果你愿意，我还可以继续做成更完整的新手向导。",
            "/guide",
        ):
            with ui.element("div").classes("ag-grid-3 w-full"):
                with ui.card().classes("ag-card p-5"):
                    ui.label(t("普通模式")).classes("ag-section-title")
                    ui.label(t("适合常规综述。先分析文献，再生成大纲和正文。")).classes("ag-subtle")
                with ui.card().classes("ag-card p-5"):
                    ui.label(t("概念增强模式")).classes("ag-section-title")
                    ui.label(t("适合围绕某个概念做更聚焦的抽取与比较。")).classes("ag-subtle")
                with ui.card().classes("ag-card p-5"):
                    ui.label(t("自由模式")).classes("ag-section-title")
                    ui.label(t("适合先说出你的研究想法，让系统先整理成更好的 prompt profile。")).classes("ag-subtle")
            with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
                ui.label(t("关于 OCR 和预处理")).classes("ag-section-title")
                ui.label(t("默认不是全量 OCR。系统会先判断 PDF 是否有可用文本，再只对异常页触发 OCR。这样更省性能，也更适合普通电脑。")).classes("ag-subtle")
                ui.label(t("如果后续你想继续增强前端体验，最自然的下一步会是“Python 后端 + JavaScript 前端”。当前这个 NiceGUI 版本则更适合快速把本地工具做成可用的网页工作台。")).classes("ag-subtle q-mt-sm")

    ui.run(
        host="127.0.0.1",
        port=port,
        title="Auto Generate GUI",
        reload=reload,
        show=show,
    )
