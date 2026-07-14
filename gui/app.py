"""NiceGUI-powered local workspace for auto-generate."""

from __future__ import annotations

import asyncio
import configparser
import json
import os
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from free_mode.profile_manager import get_profile_path, normalize_profile
from free_mode.service import generate_free_mode_profile, plan_free_mode_chat_turn
from services.configuration_service import (
    API_ENV_MAPPING,
    MINERU_ENV_KEYS,
    PROVIDER_PRESETS,
    ensure_config_sections,
    normalize_api_base,
    normalize_for_save,
    read_env_file,
    save_config_and_env,
    test_api_endpoint,
)
from services.config_compat import apply_validation_compat_sections, read_validation_settings
from services.environment_service import (
    RuntimeEnvironment,
    detect_runtime_environment,
    recommended_conda_activate_command,
    recommended_conda_create_command,
)
from services.progress_service import ProgressTracker
from services.queue_service import PersistentQueueService, QueueState
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
        "title": "开始使用",
        "items": [
            ("总览", "/", "项目入口与第一轮路径", "home"),
            ("使用引导", "/guide", "第一次使用先看这里", "menu_book"),
        ],
    },
    {
        "title": "运行任务",
        "items": [
            ("工作台", "/workflow", "选择输入来源、运行方式与主流程", "dashboard_customize"),
            ("结果与日志", "/logs", "查看最新工作区、主要产物与日志", "receipt_long"),
        ],
    },
    {
        "title": "设置",
        "items": [
            ("环境与路径", "/setup", "基础路径与输出目录", "settings_suggest"),
            ("API 与模型", "/setup/api", "阅读、写作、大纲、自由模式与验证模型", "hub"),
            ("性能与预处理", "/setup/processing", "并发、OCR、缓存、RAG 与可选验证", "tune"),
        ],
    },
]

SEARCH_ITEMS = [
    {
        "route": "/workflow",
        "label": "工作台",
        "keywords": ["工作台", "workflow", "run", "analyze", "outline", "review", "自由模式", "free mode", "概念", "concept", "workspace"],
    },
    {
        "route": "/setup",
        "label": "环境与路径",
        "keywords": ["setup", "路径", "path", "zotero", "output", "输出", "配置"],
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
        "label": "结果与日志",
        "keywords": ["log", "logs", "日志", "output", "产物", "失败", "report", "workspace", "结果"],
    },
    {
        "route": "/guide",
        "label": "使用引导",
        "keywords": ["guide", "help", "帮助", "怎么用", "新手", "first time"],
    },
]

DEFAULT_MINERU_BASE_URL = "https://mineru.net/api/v4"

STYLE_BLOCK = """
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<style>
:root {
  --paper: #f5f1e8;
  --paper-soft: #efe8dc;
  --panel: rgba(255, 252, 247, 0.92);
  --panel-strong: rgba(255, 252, 247, 0.97);
  --ink: #202725;
  --muted: #56645f;
  --accent: #5b6d66;
  --accent-soft: #dde5df;
  --line: rgba(32, 39, 37, 0.10);
  --line-strong: rgba(32, 39, 37, 0.16);
  --shadow: 0 12px 28px rgba(31, 37, 35, 0.05);
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
  background: rgba(245, 241, 232, 0.94);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--line);
}
.ag-fixedbar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  padding: 10px 18px 8px 18px;
}
.ag-fixedbar,
.ag-fixedbar *,
.ag-topbar,
.ag-topbar * {
  color: var(--ink) !important;
}
.ag-topbar-title {
  color: #50675f !important;
  font-weight: 600;
  font-size: 1.16rem;
  letter-spacing: 0.02em;
  font-family: "Palatino Linotype", Georgia, "Times New Roman", serif;
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
  padding: 3px 10px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.48);
  border: 1px solid var(--line);
  color: var(--muted) !important;
  font-size: 0.75rem;
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
  border-radius: 12px;
  background: rgba(237, 241, 235, 0.92);
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
.ag-fixedbar .q-btn.q-btn--unelevated,
.ag-topbar .q-btn.q-btn--unelevated {
  background: var(--accent);
  border-color: rgba(91, 109, 102, 0.82);
  color: #fff !important;
}
.ag-fixedbar .q-btn.q-btn--unelevated *,
.ag-topbar .q-btn.q-btn--unelevated * {
  color: #fff !important;
}
.q-btn {
  border-radius: 12px;
  transition: background-color 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
}
.q-btn:hover {
  transform: translateY(-1px);
}
.q-field--focused .q-field__control {
  border-color: rgba(91, 109, 102, 0.42) !important;
  box-shadow: 0 0 0 3px rgba(91, 109, 102, 0.08);
}
*:focus-visible {
  outline: 2px solid rgba(91, 109, 102, 0.48);
  outline-offset: 2px;
}
.ag-drawer {
  background: rgba(249, 246, 240, 0.96);
  backdrop-filter: blur(10px);
  border-right: 1px solid var(--line);
}
.ag-page {
  max-width: 1380px;
  min-height: calc(100vh - 132px);
  margin: 162px auto 40px auto;
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
  border-radius: 18px;
  box-shadow: var(--shadow);
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
  background: rgba(232, 238, 233, 0.9);
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
  font-size: 0.78rem;
  letter-spacing: 0.06em;
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
  border-radius: 14px;
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
  border-radius: 14px;
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
  border-radius: 14px;
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
.ag-editorial-list {
  display: grid;
  gap: 14px;
}
.ag-editorial-step {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 14px;
  align-items: start;
  padding: 14px 0;
  border-top: 1px solid var(--line);
}
.ag-editorial-step:first-child {
  border-top: none;
  padding-top: 0;
}
.ag-step-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 999px;
  border: 1px solid var(--line-strong);
  background: rgba(255, 255, 255, 0.72);
  color: var(--accent);
  font-family: Georgia, "Times New Roman", serif;
}
.ag-step-title {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.02rem;
}
.ag-step-note {
  color: var(--muted);
  line-height: 1.72;
}
.ag-summary-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.ag-summary-item {
  padding: 12px 14px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.55);
  border: 1px solid var(--line);
}
.ag-ledger-list {
  display: grid;
  gap: 12px;
}
.ag-ledger-row {
  display: grid;
  gap: 12px;
  padding: 16px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.48);
  border: 1px solid var(--line);
}
.ag-ledger-row .ag-build-badge {
  align-self: start;
}
.ag-ledger-main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}
.ag-ledger-meta {
  display: grid;
  gap: 4px;
  color: var(--muted);
  font-size: 0.88rem;
  line-height: 1.55;
}
.ag-note-block {
  padding: 14px 16px;
  border-left: 3px solid rgba(91, 109, 102, 0.35);
  background: rgba(255, 255, 255, 0.44);
  border-radius: 0 14px 14px 0;
}
.ag-queue-guide {
  display: grid;
  gap: 10px;
  padding: 14px;
  border-radius: 14px;
  background: rgba(232, 238, 233, 0.58);
  border: 1px solid var(--line);
}
.ag-queue-step {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 10px;
  align-items: start;
}
.ag-queue-step-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 999px;
  background: var(--accent);
  color: #fff;
  font-size: 0.78rem;
  font-weight: 600;
}
.ag-mode-toggle,
.ag-mode-toggle .q-btn-group {
  width: 100%;
  display: grid;
  gap: 6px;
  padding: 6px;
  background: rgba(228, 235, 229, 0.82);
  border: 1px solid var(--line-strong);
  border-radius: 18px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.45);
}
.ag-mode-toggle-2,
.ag-mode-toggle-2 .q-btn-group {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.ag-mode-toggle-3,
.ag-mode-toggle-3 .q-btn-group {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.ag-mode-toggle .q-btn {
  width: 100%;
  min-width: 0;
  min-height: 48px;
  border-radius: 14px !important;
  padding: 4px 14px;
  background: transparent;
  color: var(--muted);
  box-shadow: none !important;
}
.ag-mode-toggle .q-btn::before {
  box-shadow: none !important;
}
.ag-mode-toggle .q-btn .q-btn__content {
  width: 100%;
  justify-content: center;
  white-space: normal;
  text-align: center;
  line-height: 1.38;
  font-weight: 600;
}
.ag-mode-toggle .q-btn[aria-pressed="true"],
.ag-mode-toggle .q-btn.q-btn--active,
.ag-mode-toggle .q-btn.q-btn--outline.q-btn--active {
  background: rgba(48, 58, 54, 0.92) !important;
  color: #f7f4ed !important;
}
.ag-toggle-ledger {
  display: grid;
  gap: 12px;
  margin-top: 14px;
}
.ag-toggle-ledger-2 {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.ag-toggle-ledger-3 {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.ag-toggle-note {
  display: grid;
  gap: 6px;
  min-height: 96px;
  padding: 14px 16px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.52);
  border: 1px solid var(--line);
}
.ag-wrap-note {
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: break-word;
  line-height: 1.72;
}
.ag-status-block {
  display: grid;
  gap: 6px;
  min-height: 92px;
  padding: 14px 16px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.54);
  border: 1px solid var(--line);
}
.ag-planner-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}
.ag-planner-output .q-field__control,
.ag-planner-output .q-field__control-container {
  min-height: 176px;
  align-items: stretch;
}
.ag-planner-output textarea.q-field__native {
  min-height: 132px !important;
  line-height: 1.68;
  overflow-y: auto !important;
}
.ag-chat-composer {
  display: grid;
  gap: 12px;
  padding: 14px;
  border-radius: 14px;
  background: rgba(232, 238, 233, 0.66);
  border: 1px solid var(--line-strong);
}
.ag-chat-composer-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.ag-chat-input .q-field__control {
  background: rgba(255, 255, 255, 0.76);
}
.ag-kv-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.ag-kv-item {
  display: grid;
  gap: 4px;
  padding: 14px 16px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.55);
  border: 1px solid var(--line);
}
.ag-section-divider {
  height: 1px;
  background: var(--line);
  width: 100%;
}
@media (max-width: 1100px) {
  .ag-grid-2, .ag-grid-3, .ag-grid-compact, .ag-mini-grid, .ag-workflow-shell, .ag-mode-grid, .ag-action-grid, .ag-summary-strip, .ag-toggle-ledger, .ag-kv-grid, .ag-planner-grid {
    grid-template-columns: 1fr;
  }
  .ag-mode-toggle .q-btn-group {
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


def _latest_log_excerpt(
    language: str = "zh-CN",
    *,
    output_root: str | Path | None = None,
    project_name: str = "",
    queue_service: Any | None = None,
) -> Tuple[str, str]:
    for candidate in _candidate_job_logs(output_root=output_root, project_name=project_name, queue_service=queue_service):
        path = Path(candidate)
        if path.exists():
            return _read_log_excerpt(path, language)

    logs_dir = REPO_ROOT / "logs"
    if not logs_dir.exists():
        return "", translate(language, "暂无日志文件。")

    log_files = sorted(logs_dir.glob("*.log"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not log_files:
        return "", translate(language, "暂无日志文件。")

    latest = log_files[0]
    return _read_log_excerpt(latest, language)


def _read_log_excerpt(path: Path, language: str) -> Tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        excerpt = "\n".join(lines[-60:])
        return str(path), excerpt
    except Exception as exc:  # pragma: no cover - defensive.
        return str(path), translate(language, "无法读取日志：{exc}").format(exc=exc)


def _candidate_job_logs(
    *,
    output_root: str | Path | None,
    project_name: str,
    queue_service: Any | None,
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def append(path_value: Any) -> None:
        raw = str(path_value or "").strip()
        if not raw:
            return
        path = Path(raw)
        try:
            key = path.resolve()
        except Exception:
            key = path.absolute()
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    root = Path(output_root or REPO_ROOT / "output")
    if project_name:
        pointer_path = root / project_name / "_latest_job.json"
        payload = _read_json_payload(pointer_path)
        workspace_path = str((payload or {}).get("workspace_path") or "")
        if workspace_path:
            append(Path(workspace_path) / "logs" / "job.log")

    runtimes = getattr(queue_service, "_runtimes", {}) if queue_service is not None else {}
    runtime_items = list(runtimes.values()) if isinstance(runtimes, dict) else []
    runtime_items.sort(key=lambda item: str(getattr(item, "completed_at", None) or getattr(item, "started_at", None) or ""), reverse=True)
    for runtime in runtime_items:
        append(getattr(runtime, "log_path", ""))

    if root.exists():
        workspace_logs = sorted(root.glob("*__*/logs/job.log"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
        for path in workspace_logs:
            append(path)

    return candidates


def _read_json_payload(path: Path) -> Dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _workspace_primary_artifacts(workspace_path: str, project_name: str) -> list[tuple[str, str]]:
    workspace = Path(workspace_path)
    if not workspace.exists():
        return []

    artifact_candidates: list[tuple[str, Path]] = [
        ("结构化摘要", workspace / "artifacts" / f"{project_name}_summaries.json"),
        ("综述大纲", workspace / "artifacts" / f"{project_name}_literature_review_outline.md"),
        ("注册表", workspace / "artifact_registry.json"),
    ]

    reports_dir = workspace / "reports"
    if reports_dir.exists():
        for label, pattern in (
            ("分析表", "*_analyzed_papers.xlsx"),
            ("综述文档", "*_literature_review.docx"),
            ("失败报告", "*_failed_papers_report.txt"),
            ("验证报告", "*validation*.json"),
        ):
            match = next(iter(sorted(reports_dir.glob(pattern))), None)
            if match is not None:
                artifact_candidates.append((label, match))

    return [(label, str(path)) for label, path in artifact_candidates if path.exists()]


def _latest_workspace_snapshot(output_root: str, preferred_project: str = "") -> Dict[str, Any] | None:
    output_dir = Path(output_root).expanduser()
    if not output_dir.exists():
        return None

    pointer_candidates: list[Dict[str, Any]] = []
    preferred_project = str(preferred_project or "").strip()

    if preferred_project:
        preferred_pointer = output_dir / preferred_project / "_latest_job.json"
        payload = _read_json_payload(preferred_pointer)
        if payload:
            payload["_pointer_path"] = str(preferred_pointer)
            pointer_candidates.append(payload)

    for pointer_path in output_dir.glob("*/_latest_job.json"):
        if pointer_path.parent.name.startswith("_"):
            continue
        payload = _read_json_payload(pointer_path)
        if not payload:
            continue
        payload["_pointer_path"] = str(pointer_path)
        if preferred_project and str(payload.get("project_name") or "") == preferred_project:
            continue
        pointer_candidates.append(payload)

    def _sort_key(item: Dict[str, Any]) -> str:
        return str(item.get("updated_at") or "")

    if pointer_candidates:
        pointer_candidates.sort(key=_sort_key, reverse=True)
        selected = pointer_candidates[0]
        workspace_path = str(selected.get("workspace_path") or "")
        project_name = str(selected.get("project_name") or "")
        if workspace_path and Path(workspace_path).exists():
            return {
                "project_name": project_name,
                "job_id": str(selected.get("job_id") or ""),
                "status": str(selected.get("status") or ""),
                "updated_at": str(selected.get("updated_at") or ""),
                "workspace_path": workspace_path,
                "artifact_registry_path": str(selected.get("artifact_registry_path") or ""),
                "pointer_path": str(selected.get("_pointer_path") or ""),
                "artifacts": _workspace_primary_artifacts(workspace_path, project_name),
            }

    fallback_workspaces = sorted(
        [
            path
            for path in output_dir.glob("*__*")
            if path.is_dir()
        ],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not fallback_workspaces:
        return None

    workspace = fallback_workspaces[0]
    project_name = workspace.name.split("__", 1)[0]
    return {
        "project_name": project_name,
        "job_id": workspace.name.split("__", 1)[1] if "__" in workspace.name else "",
        "status": "unknown",
        "updated_at": "",
        "workspace_path": str(workspace),
        "artifact_registry_path": str(workspace / "artifact_registry.json"),
        "pointer_path": "",
        "artifacts": _workspace_primary_artifacts(str(workspace), project_name),
    }


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
        self._registered_disconnect_clients: set[int] = set()
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
        self.queue_processor_running = False
        self._queue_processor_task: asyncio.Task[Any] | None = None
        self.free_mode_chat_input = ""
        self.free_mode_messages: list[Dict[str, str]] = []
        self.free_mode_profile_draft: Dict[str, Any] = normalize_profile(None)
        self.free_mode_missing_information: list[str] = []
        self.free_mode_profile_path = ""
        self.free_mode_ready_to_apply = False
        self.free_mode_busy = False
        self.status_message = self.t("工作台已就绪。先完成设置，再按“输入来源 → 运行方式 → 主流程”开始第一轮。")
        validation_settings = read_validation_settings(self.sections)
        mineru_base_url = self.env_values.get("MINERU_BASE_URL", DEFAULT_MINERU_BASE_URL)
        mineru_model_version = self.env_values.get("MINERU_MODEL_VERSION", "vlm") or "vlm"
        allow_local_parse_fallback = str(
            self.env_values.get("ALLOW_LOCAL_PARSE_FALLBACK", "true")
        ).strip().lower() not in {"0", "false", "no", "off"}
        default_input_mode = "zotero" if self.sections["Paths"].get("zotero_report", "").strip() else "pdf"
        self.state: Dict[str, Any] = {
            "paths": {
                "zotero_report": self.sections["Paths"].get("zotero_report", ""),
                "library_path": self.sections["Paths"].get("library_path", ""),
                "output_path": self.sections["Paths"].get("output_path", "./output"),
            },
            "performance": {
                "max_workers": self.sections["Performance"].get("max_workers", "3"),
                "api_retry_attempts": self.sections["Performance"].get("api_retry_attempts", "5"),
                "enable_stage2_validation": validation_settings.stage2_enabled,
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
                "parser_mode": self.sections["Preprocess"].get("parser_mode", "local"),
                "primary_parser": self.sections["Preprocess"].get("primary_parser", "local"),
                "fallback_parser": self.sections["Preprocess"].get("fallback_parser", "local"),
                "extractor_profile": self.sections["Preprocess"].get("extractor_profile", "auto"),
                "ocr_mode": self.sections["Preprocess"].get("ocr_mode", "auto"),
                "ocr_languages": self.sections["Preprocess"].get("ocr_languages", "eng"),
                "force_rebuild": self.sections["Preprocess"].get("force_rebuild", "false") == "true",
                "enable_local_rag": self.sections["Preprocess"].get("enable_local_rag", "false") == "true",
                "rag_backend": self.sections["Preprocess"].get("rag_backend", "chroma"),
            },
            "mineru": {
                "base_url": mineru_base_url,
                "api_token": self.env_values.get("MINERU_API_TOKEN", ""),
                "model_version": mineru_model_version,
                "allow_local_parse_fallback": allow_local_parse_fallback,
            },
            "workflow": {
                "project_name": "",
                "input_mode": default_input_mode,
                "work_mode": "normal",
                "pdf_folder": "",
                "summary_file": "",
                "summary_sources": "",
                "reuse_stage1": True,
                "reuse_summary_files": "",
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
                "proxy_mode": self.sections.get(section_name, {}).get("proxy_mode", "environment") or "environment",
            }
        
        # 初始化队列服务
        self._queue_service: Optional[Any] = None
        self._queue_runner: Optional[Any] = None
        self._init_queue_service()

    def _init_queue_service(self) -> None:
        """初始化队列服务"""
        try:
            output_path = Path(self.state["paths"]["output_path"])
            queue_file_path = output_path / "_queue" / "queue.json"
            self._queue_service = PersistentQueueService(queue_file_path)
            self._queue_runner = None
        except Exception:
            self._queue_service = None

    def refresh_queue(self, *, notify_user: bool = True) -> None:
        """刷新队列状态"""
        if self._queue_service is None:
            self._init_queue_service()
        if self._queue_service:
            self.set_status(self.t("队列已刷新"))
            if notify_user:
                ui.notify(self.t("队列已刷新"))
        else:
            if notify_user:
                ui.notify(self.t("队列服务未初始化"), type="warning")

    def clear_completed_jobs(self) -> None:
        """清空已完成的任务"""
        if self._queue_service:
            try:
                jobs = self._queue_service.list_jobs()
                count = 0
                for job in jobs:
                    runtime = self._queue_service.get_job_runtime(job.job_id)
                    if runtime and runtime.state == QueueState.COMPLETED:
                        self._queue_service.remove_job(job.job_id)
                        count += 1
                ui.notify(self.tf("已清空已完成任务 {count}", count=count))
            except Exception as e:
                ui.notify(self.tf("清空失败: {e}", e=str(e)), type="negative")
        else:
            ui.notify(self.t("队列服务未初始化"), type="warning")

    def run_queue(self) -> None:
        """兼容入口：启动 GUI 后台串行队列处理器。"""
        if self._schedule_queue_processor():
            self.notify(self.t("开始运行队列任务..."), color="positive")
        else:
            self.notify(self.t("队列服务未初始化，或队列处理器已经在运行。"), color="info")

    def _queue_position(self, job_id: str) -> int | None:
        if not self._queue_service:
            return None
        try:
            pending_ids = [job.job_id for job in self._queue_service.list_jobs_by_state(QueueState.PENDING)]
        except Exception:
            return None
        try:
            return pending_ids.index(job_id) + 1
        except ValueError:
            return None

    def _schedule_queue_processor(self) -> bool:
        """Start one background serial queue processor if none is active."""
        runner = self._ensure_queue_runner()
        if runner is None:
            return False
        if self.queue_processor_running:
            return False
        if self._queue_processor_task is not None and not self._queue_processor_task.done():
            return False
        drain_coro = self._drain_queue_processor()
        try:
            self._queue_processor_task = asyncio.create_task(drain_coro)
        except RuntimeError:
            drain_coro.close()
            return False
        return True

    async def _drain_queue_processor(self) -> None:
        runner = self._ensure_queue_runner()
        if runner is None:
            return
        self.queue_processor_running = True
        self.progress_tracker = ProgressTracker()
        self.progress_tracker.reset(
            task_type=self.t("队列任务"),
            stage="queue",
            message=self.t("队列正在后台串行处理。你可以继续配置下一个任务。"),
            indeterminate=True,
        )
        self.progress_snapshot = self.progress_tracker.snapshot()
        self.set_status(self.t("队列正在后台串行处理。你可以继续配置下一个任务。"))
        self.update_progress_widgets()
        try:
            await asyncio.to_thread(runner.run)
            self.refresh_queue(notify_user=False)
            self.refresh_logs()
            pending_count = 0
            if self._queue_service:
                pending_count = len(self._queue_service.list_jobs_by_state(QueueState.PENDING))
            if pending_count:
                self.set_status(self.tf("队列仍有 {count} 个待处理任务，正在继续。", count=pending_count))
            else:
                if self.progress_tracker is not None:
                    self.progress_tracker.finish(success=True, message=self.t("队列后台处理完成。"))
                    self.progress_snapshot = self.progress_tracker.snapshot()
                self.set_status(self.t("队列后台处理完成。"))
        except Exception as e:
            if self.progress_tracker is not None:
                self.progress_tracker.finish(success=False, message=self.tf("队列运行失败: {e}", e=str(e)))
                self.progress_snapshot = self.progress_tracker.snapshot()
            self.set_status(self.tf("队列运行失败: {e}", e=str(e)))
            self.notify(self.tf("队列运行失败: {e}", e=str(e)), color="negative", multi_line=True)
        finally:
            self.queue_processor_running = False
            self._queue_processor_task = None
            self._queue_runner = None
            self.update_progress_widgets()
            self.refresh_queue(notify_user=False)
            if self._queue_service and self._queue_service.list_jobs_by_state(QueueState.PENDING):
                self._schedule_queue_processor()

    def retry_job(self, job_id: str) -> None:
        """重试指定任务"""
        if self._queue_service:
            try:
                runtime = self._queue_service.get_job_runtime(job_id)
                if runtime and runtime.state in (QueueState.FAILED, QueueState.CANCELLED):
                    self._queue_service.reset_job(job_id)
                    ui.notify(self.tf("任务已重置并将重试: {job_id}", job_id=job_id), type="positive")
                else:
                    ui.notify(self.t("只能重试失败或已取消的任务"), type="warning")
            except Exception as e:
                ui.notify(self.tf("重试任务失败: {e}", e=str(e)), type="negative")
        else:
            ui.notify(self.t("队列服务未初始化"), type="warning")

    def cancel_job(self, job_id: str) -> None:
        """取消指定任务"""
        if self._queue_service:
            try:
                # 如果queue_runner存在，使用它的cancel_job方法
                if self._queue_runner:
                    if self._queue_runner.cancel_job(job_id):
                        ui.notify(self.tf("任务已取消: {job_id}", job_id=job_id), type="positive")
                    else:
                        ui.notify(self.t("只能取消运行中的任务"), type="warning")
                else:
                    # 如果queue_runner不存在，只标记状态
                    runtime = self._queue_service.get_job_runtime(job_id)
                    if runtime and runtime.state == QueueState.RUNNING:
                        self._queue_service.update_job_state(job_id, QueueState.CANCELLED)
                        ui.notify(self.tf("任务已标记为取消: {job_id}", job_id=job_id), type="positive")
                    else:
                        ui.notify(self.t("只能取消运行中的任务"), type="warning")
            except Exception as e:
                ui.notify(self.tf("取消任务失败: {e}", e=str(e)), type="negative")
        else:
            ui.notify(self.t("队列服务未初始化"), type="warning")

    def _build_queue_job_spec(
        self,
        project_name: str,
        pdf_folder: str,
        zotero_report: str,
        action: str,
        *,
        input_mode: str | None = None,
        work_mode: str | None = None,
    ) -> Any:
        from services.queue_service import QueueJobSpec, create_queue_job_id

        workflow_state = self.state["workflow"]
        resolved_input_mode = str(input_mode or workflow_state.get("input_mode") or "pdf")
        resolved_work_mode = str(work_mode or workflow_state.get("work_mode") or "normal")
        library_path = str(self.state["paths"].get("library_path") or "").strip() or None if resolved_input_mode == "zotero" else None
        effective_pdf_folder = str(pdf_folder or "").strip() or None if resolved_input_mode == "pdf" else None
        effective_zotero_report = str(zotero_report or "").strip() or None if resolved_input_mode == "zotero" else None
        free_mode_profile = self.free_mode_profile_path or None if resolved_work_mode == "free" else None
        free_mode_idea = None
        if resolved_work_mode == "free" and not free_mode_profile:
            free_mode_idea = str(workflow_state.get("free_mode_idea") or "").strip() or None
        generate_section = None
        if action == "generate_section":
            section_number_raw = str(workflow_state.get("section_number") or "").strip()
            if section_number_raw.isdigit() and int(section_number_raw) > 0:
                generate_section = int(section_number_raw)

        stage1_reuse_actions = {"analyze", "run_all"}
        reuse_stage1_enabled = bool(workflow_state.get("reuse_stage1")) and action in stage1_reuse_actions
        reuse_summary_files = [
            item.strip()
            for item in str(workflow_state.get("reuse_summary_files") or "").splitlines()
            if item.strip()
        ] if reuse_stage1_enabled else []
        summary_sources = [
            item.strip()
            for item in str(workflow_state.get("summary_sources") or "").splitlines()
            if item.strip()
        ]
        parameters = {
            "action": action,
            "project_name": project_name,
            "pdf_folder": effective_pdf_folder,
            "zotero_report": effective_zotero_report,
            "library_path": library_path,
            "config": self.config_path,
            "gui": True,
            "run_all": action == "run_all",
            "analyze_only": action == "analyze",
            "generate_outline": action == "outline",
            "generate_review": action == "review",
            "generate_section": generate_section,
            "validate_review": action == "validate",
            "retry_failed": action == "retry_failed",
            "retry_review_failed": action == "retry_review_failed",
            "concept": str(workflow_state.get("concept") or "").strip() or None if resolved_work_mode == "concept" else None,
            "free_mode_profile": free_mode_profile,
            "free_mode_idea": free_mode_idea,
            "summary_file": str(workflow_state.get("summary_file") or "").strip() or None,
            "summary_sources": summary_sources,
            "reuse_stage1": reuse_stage1_enabled,
            "reuse_summary_files": reuse_summary_files,
            "queue_file": str(Path(self.state["paths"]["output_path"]) / "_queue" / "queue.json"),
            "source_mode": "zotero" if effective_zotero_report else "direct",
        }
        source_snapshot = {
            "project_name": project_name,
            "input_mode": resolved_input_mode,
            "work_mode": resolved_work_mode,
            "action": action,
            "pdf_folder": effective_pdf_folder,
            "zotero_report": effective_zotero_report,
            "library_path": library_path,
            "summary_file": parameters["summary_file"],
            "summary_sources": list(summary_sources),
            "reuse_stage1": parameters["reuse_stage1"],
            "reuse_summary_files": list(reuse_summary_files),
            "concept": parameters["concept"],
            "free_mode_profile": free_mode_profile,
            "free_mode_idea": free_mode_idea,
            "generate_section": generate_section,
        }

        return QueueJobSpec(
            job_id=create_queue_job_id(),
            job_type=action,
            project_name=project_name,
            parameters=parameters,
            source_snapshot=source_snapshot,
        )

    def _ensure_queue_runner(self) -> Optional[Any]:
        if not self._queue_service:
            return None
        if self._queue_runner is None:
            from services.job_runner import JobRunner
            from services.queue_service import QueueRunner

            self._queue_runner = QueueRunner(self._queue_service, JobRunner())
        return self._queue_runner

    def add_job_to_queue(
        self,
        project_name: str,
        pdf_folder: str,
        zotero_report: str,
        action: str,
        *,
        input_mode: str | None = None,
        work_mode: str | None = None,
    ) -> Optional[str]:
        """Add a job to the persistent queue and return the created job id."""
        if not self._queue_service:
            self.notify(self.t("队列服务未初始化"), color="warning")
            return None

        try:
            spec = self._build_queue_job_spec(
                project_name,
                pdf_folder,
                zotero_report,
                action,
                input_mode=input_mode,
                work_mode=work_mode,
            )
            job_id = self._queue_service.add_job(spec)
            self.refresh_queue(notify_user=False)
            self.notify(self.tf("Added job to queue: {job_id}", job_id=job_id), color="positive")
            return job_id
        except Exception as e:
            self.notify(self.tf("Failed to add job to queue: {error}", error=str(e)), color="negative", multi_line=True)
            return None

    def remove_job(self, job_id: str) -> None:
        """删除指定任务"""
        if self._queue_service:
            try:
                runtime = self._queue_service.get_job_runtime(job_id)
                if runtime and runtime.state == QueueState.RUNNING:
                    ui.notify(self.t("不能删除运行中的任务"), type="warning")
                    return
                self._queue_service.remove_job(job_id)
                ui.notify(self.tf("任务已删除: {job_id}", job_id=job_id), type="positive")
            except Exception as e:
                ui.notify(self.tf("删除任务失败: {e}", e=str(e)), type="negative")
        else:
            ui.notify(self.t("队列服务未初始化"), type="warning")

    def reorder_jobs(self, job_ids: list[str]) -> None:
        """重排任务顺序"""
        if self._queue_service:
            try:
                self._queue_service.reorder_jobs(job_ids)
                ui.notify(self.t("任务顺序已更新"), type="positive")
            except Exception as e:
                ui.notify(self.tf("重排任务失败: {e}", e=str(e)), type="negative")
        else:
            ui.notify(self.t("队列服务未初始化"), type="warning")

    def move_queue_job(self, job_id: str, offset: int) -> None:
        """按单步偏移量移动任务，避免出现“已选中”但实际上没有选择器的迷惑交互。"""
        if not self._queue_service:
            ui.notify(self.t("队列服务未初始化"), type="warning")
            return

        try:
            ordered_ids = [job.job_id for job in self._queue_service.list_jobs()]
            current_index = ordered_ids.index(job_id)
        except ValueError:
            return
        except Exception as e:
            ui.notify(self.tf("调整任务顺序失败: {e}", e=str(e)), type="negative")
            return

        target_index = current_index + offset
        if target_index < 0 or target_index >= len(ordered_ids):
            return

        moving_job_id = ordered_ids.pop(current_index)
        ordered_ids.insert(target_index, moving_job_id)
        self.reorder_jobs(ordered_ids)

    def t(self, key: str) -> str:
        return translate(self.language, key)

    def tf(self, key: str, **kwargs: Any) -> str:
        return self.t(key).format(**kwargs)

    def action_label(self, action: str) -> str:
        return action_label(self.language, action)

    def register_status_label(self, label: Any) -> None:
        self._prune_stale_bindings()
        self.bindings.status_labels.append(label)
        self._safe_apply(label, lambda element: element.set_text(self.status_message))

    @staticmethod
    def _is_deleted_client_error(exc: BaseException) -> bool:
        return isinstance(exc, RuntimeError) and "client this element belongs to has been deleted" in str(exc).lower()

    @classmethod
    def _is_stale_element(cls, element: Any) -> bool:
        if element is None:
            return True
        try:
            if bool(getattr(element, "is_deleted", False)):
                return True
        except Exception:
            return True
        try:
            getattr(element, "client")
        except AttributeError:
            return False
        except RuntimeError as exc:
            if cls._is_deleted_client_error(exc):
                return True
            raise
        except Exception:
            return True
        return False

    @classmethod
    def _safe_apply(cls, element: Any, updater: Callable[[Any], None]) -> bool:
        if cls._is_stale_element(element):
            return False
        try:
            updater(element)
            return True
        except RuntimeError as exc:
            if cls._is_deleted_client_error(exc):
                return False
            raise

    @classmethod
    def _safe_update_bound_list(cls, elements: list[Any], updater: Callable[[Any], None]) -> None:
        live_elements: list[Any] = []
        for element in elements:
            if cls._safe_apply(element, updater):
                live_elements.append(element)
        elements[:] = live_elements

    def _prune_stale_bindings(self) -> None:
        for binding_field in fields(self.bindings):
            bound_value = getattr(self.bindings, binding_field.name)
            if isinstance(bound_value, list):
                self._safe_update_bound_list(bound_value, lambda _element: None)
            elif isinstance(bound_value, dict):
                stale_keys = [key for key, element in list(bound_value.items()) if self._is_stale_element(element)]
                for key in stale_keys:
                    bound_value.pop(key, None)

    def register_client(self, client: Any) -> None:
        self.client = client
        self._prune_stale_bindings()
        if client is None:
            return
        client_key = id(client)
        if client_key in self._registered_disconnect_clients:
            return
        self._registered_disconnect_clients.add(client_key)
        try:
            client.on_disconnect(lambda *_args: self._prune_stale_bindings())
        except Exception:
            self._registered_disconnect_clients.discard(client_key)

    def notify(
        self,
        message: str,
        *,
        color: str = "positive",
        multi_line: bool = False,
        close_button: bool | str = True,
    ) -> None:
        client = self.client
        if client is None:
            return
        try:
            with client:
                ui.notify(message, color=color, multi_line=multi_line, close_button=close_button)
        except RuntimeError as exc:
            if self._is_deleted_client_error(exc):
                if self.client is client:
                    self.client = None
                self._prune_stale_bindings()
                return
            raise

    def register_log_widgets(self, path_label: Any, log_view: Any) -> None:
        self._prune_stale_bindings()
        self.bindings.log_path_labels.append(path_label)
        self.bindings.log_views.append(log_view)
        self._safe_apply(path_label, lambda element: element.set_text(self.latest_log_path or self.t("暂无日志文件。")))
        self._safe_apply(log_view, lambda element: element.set_value(self.latest_log_excerpt))

    def register_action_button(self, button: Any) -> None:
        self._prune_stale_bindings()
        self.bindings.action_buttons.append(button)
        if self.workflow_running:
            self._safe_apply(button, lambda element: element.disable())

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
        self._prune_stale_bindings()
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
        self._prune_stale_bindings()
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
        self._prune_stale_bindings()
        self.bindings.api_feedback_boxes[section_name] = box
        self.bindings.api_feedback_labels[section_name] = label
        self.bindings.api_feedback_icons[section_name] = icon

    def hide_api_feedback(self, section_name: str) -> None:
        self._prune_stale_bindings()
        box = self.bindings.api_feedback_boxes.get(section_name)
        if box is not None:
            if not self._safe_apply(box, lambda element: element.classes(add="hidden", remove="flex")):
                self.bindings.api_feedback_boxes.pop(section_name, None)

    def show_api_feedback(self, section_name: str, message: str, *, tone: str = "info") -> None:
        self._prune_stale_bindings()
        box = self.bindings.api_feedback_boxes.get(section_name)
        label = self.bindings.api_feedback_labels.get(section_name)
        icon = self.bindings.api_feedback_icons.get(section_name)
        if box is None or label is None or icon is None:
            return

        for klass in ("ag-inline-alert-positive", "ag-inline-alert-negative", "ag-inline-alert-warning", "ag-inline-alert-info"):
            self._safe_apply(box, lambda element, klass=klass: element.classes(remove=klass))
        self._safe_apply(box, lambda element: element.classes(remove="hidden", add=f"flex ag-inline-alert-{tone}"))
        self._safe_apply(label, lambda element: element.set_text(message))
        self._safe_apply(icon, lambda element: setattr(element, "name", {
            "positive": "task_alt",
            "negative": "error",
            "warning": "warning",
            "info": "info",
        }.get(tone, "info")))

    def set_status(self, message: str) -> None:
        self.status_message = message
        self._safe_update_bound_list(self.bindings.status_labels, lambda element: element.set_text(message))

    def set_workflow_running(self, running: bool) -> None:
        self.workflow_running = running
        self._safe_update_bound_list(
            self.bindings.action_buttons,
            lambda element: element.disable() if running else element.enable(),
        )
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

    @staticmethod
    def _compact_path_for_ui(raw_path: str, *, head: int = 24, tail: int = 32) -> str:
        value = str(raw_path or "").strip()
        if len(value) <= head + tail + 1:
            return value
        return f"{value[:head]}…{value[-tail:]}"

    def _mineru_token_state_text(self) -> str:
        if str(self.state["mineru"]["api_token"] or "").strip():
            return self.t("MinerU token 已填写。")
        return self.t("MinerU token 还没有填写。")

    def _mineru_runtime_state_text(self) -> str:
        parser_mode = str(self.state["preprocess"]["parser_mode"] or "local").strip().lower() or "local"
        token_present = bool(str(self.state["mineru"]["api_token"] or "").strip())
        allow_local_fallback = bool(self.state["mineru"]["allow_local_parse_fallback"])
        if parser_mode == "local":
            return self.t("当前 parser mode 是 local：即使保存了 MinerU token，运行时也只会走本地解析链。")
        if parser_mode == "hybrid":
            if token_present:
                return self.t("当前 parser mode 是 hybrid：系统会先跑本地基线，只有质量不佳时才会尝试 MinerU。")
            return self.t("当前 parser mode 是 hybrid，但还没有 MinerU token，因此最终仍只会保留本地解析。")
        if parser_mode == "remote":
            if token_present:
                if allow_local_fallback:
                    return self.t("当前 parser mode 是 remote：会直接请求 MinerU；远程失败时仍允许回退到本地解析。")
                return self.t("当前 parser mode 是 remote：会直接请求 MinerU；你已经关闭本地回退，所以远程不可用时会直接失败。")
            return self.t("当前 parser mode 是 remote，但还没有 MinerU token，因此远程解析不会真正发起。")
        if token_present:
            if allow_local_fallback:
                return self.t("当前 parser mode 是 remote_first：会优先尝试 MinerU，失败后允许切回本地解析。")
            return self.t("当前 parser mode 是 remote_first：会优先尝试 MinerU；你已经关闭本地回退，所以远程失败时会直接终止。")
        return self.t("当前 parser mode 是 remote_first，但还没有 MinerU token，因此最终仍会落回本地解析。")

    def _collect_extra_env_values(self) -> Dict[str, str]:
        mineru_state = self.state["mineru"]
        return {
            "MINERU_BASE_URL": str(mineru_state["base_url"] or DEFAULT_MINERU_BASE_URL).strip().rstrip("/") or DEFAULT_MINERU_BASE_URL,
            "MINERU_API_TOKEN": str(mineru_state["api_token"] or "").strip(),
            "MINERU_MODEL_VERSION": str(mineru_state["model_version"] or "vlm").strip() or "vlm",
            "ALLOW_LOCAL_PARSE_FALLBACK": "true" if mineru_state["allow_local_parse_fallback"] else "false",
        }

    def _sync_env_values_from_disk(self) -> None:
        self.env_values = read_env_file(self.env_path)
        for env_key in [*API_ENV_MAPPING.values(), *MINERU_ENV_KEYS]:
            value = str(self.env_values.get(env_key, ""))
            if value:
                os.environ[env_key] = value
            else:
                os.environ.pop(env_key, None)

    def _free_mode_status_text(self) -> str:
        if self.free_mode_busy:
            return self.t("自由模式正在整理你的想法…")
        if self.free_mode_profile_path:
            return self.tf(
                "自由模式已应用到本次任务：{target}",
                target=self._compact_path_for_ui(self.free_mode_profile_path),
            )
        if self.free_mode_ready_to_apply and self.free_mode_messages:
            return self.t("当前规划已经比较完整，可以直接应用到本次任务。")
        if self.free_mode_messages:
            return self.t("当前规划还在澄清阶段，你可以继续补充，也可以先应用草案。")
        return self.t("先告诉规划助手你想写什么，它会边聊边帮你收束成适合综述流程的 prompt。")

    def _free_mode_hint_text(self) -> str:
        if self.free_mode_busy:
            return self.t("本轮对话返回后，这里会更新仍需补充的信息。")
        if self.free_mode_missing_information:
            items = "；".join(str(item).strip() for item in self.free_mode_missing_information[:3] if str(item).strip())
            return self.tf("还建议再确认这些点：{items}", items=items)
        if self.free_mode_profile_path:
            return self.tf(
                "后续运行会优先使用这份已应用的自由模式 profile。完整路径：{target}",
                target=self.free_mode_profile_path,
            )
        return self.t("例如：我想围绕概念 A 如何推导到概念 B 来写综述，重点比较变量链路、理论解释和 research gap。")

    def update_free_mode_widgets(self) -> None:
        transcript_text = self._format_free_mode_transcript()
        profile_text = self._format_free_mode_profile()
        status_text = self._free_mode_status_text()
        hint_text = self._free_mode_hint_text()
        disable_controls = self.workflow_running or self.free_mode_busy
        can_apply = (not disable_controls) and bool(self.free_mode_messages)

        self._safe_update_bound_list(self.bindings.free_mode_transcript_views, lambda element: element.set_value(transcript_text))
        self._safe_update_bound_list(self.bindings.free_mode_profile_views, lambda element: element.set_value(profile_text))
        self._safe_update_bound_list(self.bindings.free_mode_status_labels, lambda element: element.set_text(status_text))
        self._safe_update_bound_list(self.bindings.free_mode_hint_labels, lambda element: element.set_text(hint_text))
        self._safe_update_bound_list(
            self.bindings.free_mode_send_buttons,
            lambda element: element.disable() if disable_controls else element.enable(),
        )
        self._safe_update_bound_list(
            self.bindings.free_mode_reset_buttons,
            lambda element: element.disable() if disable_controls else element.enable(),
        )
        self._safe_update_bound_list(
            self.bindings.free_mode_apply_buttons,
            lambda element: element.enable() if can_apply else element.disable(),
        )

    def _collect_config_payload(self) -> tuple[Dict[str, Dict[str, str]], Dict[str, str], Dict[str, str]]:
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
            }
        )
        updated_sections.setdefault("Validation", {})
        updated_sections["Validation"].update(
            {
                "stage1_enabled": "false",
                "stage2_enabled": "true" if self.state["performance"]["enable_stage2_validation"] else "false",
            }
        )
        updated_sections = apply_validation_compat_sections(updated_sections)
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
                "parser_mode": self.state["preprocess"]["parser_mode"],
                "primary_parser": self.state["preprocess"]["primary_parser"],
                "fallback_parser": self.state["preprocess"]["fallback_parser"],
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
            updated_sections[section_name]["proxy_mode"] = card.get("proxy_mode", "environment") or "environment"
            api_keys[section_name] = card["api_key"]

        extra_env_values = self._collect_extra_env_values()
        return updated_sections, api_keys, extra_env_values

    def build_runtime_config(self) -> Dict[str, Dict[str, str]]:
        runtime_sections, api_keys, _extra_env_values = self._collect_config_payload()
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

        self._safe_update_bound_list(self.bindings.progress_task_labels, lambda element: element.set_text(task_text))
        self._safe_update_bound_list(self.bindings.progress_stage_labels, lambda element: element.set_text(stage_text))
        self._safe_update_bound_list(self.bindings.progress_message_labels, lambda element: element.set_text(message_text))
        self._safe_update_bound_list(self.bindings.progress_item_labels, lambda element: element.set_text(item_text))
        self._safe_update_bound_list(self.bindings.progress_counts_labels, lambda element: element.set_text(counts_text))
        self._safe_update_bound_list(self.bindings.progress_retry_labels, lambda element: element.set_text(retry_text))
        self._safe_update_bound_list(self.bindings.progress_elapsed_labels, lambda element: element.set_text(elapsed_text))

        def _update_bar(element: Any) -> None:
            if show_indeterminate:
                element.props(add="indeterminate")
                element.set_value(0)
            else:
                element.props(remove="indeterminate")
                element.set_value(progress_value)

        self._safe_update_bound_list(self.bindings.progress_overall_bars, _update_bar)
        self._safe_update_bound_list(self.bindings.progress_stage_bars, _update_bar)

    def _latest_queue_progress_snapshot(self) -> Dict[str, Any] | None:
        if not self._queue_service:
            return None
        runtimes = getattr(self._queue_service, "_runtimes", {})
        if not isinstance(runtimes, dict):
            return None
        running = [
            runtime
            for runtime in runtimes.values()
            if getattr(runtime, "state", None) == QueueState.RUNNING and getattr(runtime, "progress_snapshot", None)
        ]
        running.sort(key=lambda item: str(getattr(item, "started_at", "") or ""), reverse=True)
        if not running:
            return None
        return dict(getattr(running[0], "progress_snapshot", {}) or {})

    def refresh_progress(self) -> None:
        queue_snapshot = self._latest_queue_progress_snapshot()
        if queue_snapshot:
            self.progress_snapshot = queue_snapshot
        elif self.progress_tracker is not None:
            self.progress_snapshot = self.progress_tracker.snapshot()
            if self.progress_snapshot.get("status") in {"completed", "failed"}:
                self.set_workflow_running(False)
        self.update_progress_widgets()
        if self.workflow_running or self.queue_processor_running:
            self.refresh_logs()

    def refresh_logs(self) -> None:
        self.latest_log_path, self.latest_log_excerpt = _latest_log_excerpt(
            self.language,
            output_root=self.state["paths"].get("output_path", "./output"),
            project_name=str(self.state["workflow"].get("project_name") or "").strip(),
            queue_service=self._queue_service,
        )
        self._safe_update_bound_list(
            self.bindings.log_path_labels,
            lambda element: element.set_text(self.latest_log_path or self.t("暂无日志文件。")),
        )
        self._safe_update_bound_list(self.bindings.log_views, lambda element: element.set_value(self.latest_log_excerpt))

    def persist_config(self, *, notify_user: bool = True) -> None:
        updated_sections, api_keys, extra_env_values = self._collect_config_payload()
        normalize_for_save(updated_sections)
        save_config_and_env(
            updated_sections,
            api_keys,
            extra_env_values=extra_env_values,
            config_path=self.config_path,
            env_path=self.env_path,
        )
        self.sections = ensure_config_sections(updated_sections)
        self._sync_env_values_from_disk()
        self._init_queue_service()
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
        input_mode = str(self.state["workflow"].get("input_mode") or "pdf")
        work_mode = str(self.state["workflow"].get("work_mode") or "normal")
        pdf_folder = str(self.state["workflow"]["pdf_folder"]).strip()
        zotero_report = str(self.state["paths"]["zotero_report"]).strip()
        library_path = str(self.state["paths"]["library_path"]).strip()

        if not project_name:
            self.notify(self.t("请先填写项目名。"), color="warning")
            return False

        if work_mode == "free" and self.free_mode_messages and not self.free_mode_profile_path:
            self.notify(self.t("自由模式对话还没有应用到本次任务。请先应用当前规划，或清空对话后再运行。"), color="warning", multi_line=True)
            return False

        if action in {"analyze", "run_all"}:
            if input_mode == "pdf" and not pdf_folder:
                self.notify(self.t("当前选择的是 PDF 文件夹模式，请先填写 PDF 文件夹。"), color="warning")
                return False
            if input_mode == "zotero":
                if not zotero_report:
                    self.notify(self.t("当前选择的是 Zotero 模式，请先填写 Zotero 报告路径。"), color="warning")
                    return False
                if not library_path:
                    self.notify(self.t("Zotero 模式还需要填写 Zotero 库路径。"), color="warning")
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
            card.get("proxy_mode", "environment"),
        )
        self.show_api_feedback(section_name, message, tone="positive" if ok else "negative")
        self.notify(f"{section_name}: {message}", color="positive" if ok else "negative", multi_line=True)

    async def run_workflow(self, action: str) -> None:
        if not self.validate_workflow_request(action):
            return

        self.persist_config(notify_user=False)
        action_label_text = self.action_label(action)
        self.set_status(self.tf("正在提交 {action_label} 到后台队列……", action_label=action_label_text))
        self.progress_tracker = ProgressTracker()
        self.progress_tracker.reset(task_type=action_label_text, stage="queue", message=self.status_message, indeterminate=True)
        self.progress_snapshot = self.progress_tracker.snapshot()
        self.update_progress_widgets()

        project_name = str(self.state["workflow"]["project_name"]).strip()
        pdf_folder = str(self.state["workflow"]["pdf_folder"]).strip()
        zotero_report = str(self.state["paths"].get("zotero_report") or "").strip()

        try:
            job_id = self.add_job_to_queue(project_name, pdf_folder, zotero_report, action)
            if not job_id:
                self.progress_tracker.finish(success=False, message=self.t("任务入队失败，请检查当前输入后重试。"))
                return

            position = self._queue_position(job_id)
            if self.test_mode:
                message = self.tf("测试模式：已模拟提交 {action_label} 到后台队列。", action_label=action_label_text)
                self.progress_tracker.finish(success=True, message=message)
                self.set_status(message)
                self.notify(message, color="positive")
                return

            processor_started = self._schedule_queue_processor()
            if processor_started:
                message = self.tf("{action_label} 已加入队列并开始后台处理。", action_label=action_label_text)
            elif position:
                message = self.tf("{action_label} 已加入队列，当前排队位置：{position}。", action_label=action_label_text, position=position)
            else:
                message = self.tf("{action_label} 已加入队列。", action_label=action_label_text)
            self.progress_tracker.reset(task_type=action_label_text, stage="queue", message=message, indeterminate=True)
            self.progress_snapshot = self.progress_tracker.snapshot()
            self.set_status(message)
            self.notify(message, color="positive")
        finally:
            self.progress_snapshot = self.progress_tracker.snapshot()
            self.update_progress_widgets()
            self.refresh_logs()
            self.refresh_queue(notify_user=False)


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


def _render_workflow_queue_card(controller: WorkspaceController) -> None:
    t = controller.t

    def refresh_queue_panel(notify_user: bool = False) -> None:
        controller.refresh_queue(notify_user=notify_user)
        render_queue_panel.refresh()

    @ui.refreshable
    def render_queue_panel() -> None:
        service = controller._queue_service
        jobs = service.list_jobs() if service else []
        runtimes = {
            job.job_id: service.get_job_runtime(job.job_id) if service else None
            for job in jobs
        }
        counts = {
            QueueState.PENDING: 0,
            QueueState.RUNNING: 0,
            QueueState.COMPLETED: 0,
            QueueState.FAILED: 0,
            QueueState.CANCELLED: 0,
        }
        for runtime in runtimes.values():
            if runtime and runtime.state in counts:
                counts[runtime.state] += 1

        active_job = None
        for job in jobs:
            runtime = runtimes.get(job.job_id)
            if runtime and runtime.state == QueueState.RUNNING:
                active_job = job
                break

        state_labels = {
            QueueState.PENDING: t("待处理"),
            QueueState.RUNNING: t("运行中"),
            QueueState.COMPLETED: t("已完成"),
            QueueState.FAILED: t("失败"),
            QueueState.CANCELLED: t("已取消"),
        }

        with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
            ui.label(t("后台队列")).classes("ag-section-title")
            ui.label(t("这里不是额外的任务编辑器，而是工作台主流程按钮的后台执行区。你点“仅分析文献 / 生成大纲 / 生成全文 / 一键运行”后，任务会自动进队列并按顺序执行。")).classes("ag-subtle")
            with ui.element("div").classes("ag-queue-guide q-mt-md"):
                for step_index, step_key in [
                    ("1", "在左侧填写输入来源、项目名和运行方式。"),
                    ("2", "点击主流程操作按钮，当前任务会自动加入后台队列。"),
                    ("3", "任务进入队列后，表单不会被锁死；你可以继续配置下一项。"),
                    ("4", "如果任务失败，可以在队列列表里重试；待处理任务可以上移或下移。"),
                ]:
                    with ui.element("div").classes("ag-queue-step"):
                        ui.label(step_index).classes("ag-queue-step-index")
                        ui.label(t(step_key)).classes("ag-subtle")
            with ui.row().classes("gap-2 q-mt-md flex-wrap"):
                ui.button(t("刷新队列"), on_click=lambda: refresh_queue_panel(True)).props("outline size=sm")
                ui.button(
                    t("启动后台处理"),
                    on_click=lambda: (controller._schedule_queue_processor(), refresh_queue_panel(False)),
                ).props("unelevated size=sm")
                ui.button(
                    t("清空已完成"),
                    on_click=lambda: (controller.clear_completed_jobs(), refresh_queue_panel(False)),
                ).props("outline size=sm")

            with ui.element("div").classes("ag-summary-strip q-mt-md"):
                for label_key, value in [
                    ("运行中", counts[QueueState.RUNNING]),
                    ("待处理", counts[QueueState.PENDING]),
                    ("失败", counts[QueueState.FAILED]),
                    ("已完成", counts[QueueState.COMPLETED]),
                ]:
                    with ui.element("div").classes("ag-summary-item"):
                        ui.label(t(label_key)).classes("ag-subtle")
                        ui.label(str(value)).classes("text-body1")

            if not jobs:
                ui.label(t("暂无队列任务。先在左侧配置任务，再点击“主流程操作”里的按钮，任务就会出现在这里。")).classes("ag-subtle q-mt-md")
                return

            if active_job is not None:
                runtime = runtimes.get(active_job.job_id)
                progress_snapshot = dict(getattr(runtime, "progress_snapshot", {}) or {}) if runtime else {}
                with ui.element("div").classes("ag-note-block q-mt-md"):
                    ui.label(t("当前后台任务")).classes("ag-subtle")
                    ui.label(f"{controller.action_label(active_job.job_type)} · {active_job.project_name}").classes("text-body1")
                    ui.label(f"ID: {active_job.job_id}").classes("ag-subtle")
                    if runtime and runtime.current_stage:
                        ui.label(f"{t('当前阶段')}: {runtime.current_stage}").classes("ag-subtle")
                    if progress_snapshot:
                        message = str(progress_snapshot.get("message") or "")
                        item_label = str(progress_snapshot.get("item_label") or "")
                        counts_text = (
                            f"{int(progress_snapshot.get('success_count') or 0)} / "
                            f"{int(progress_snapshot.get('failure_count') or 0)} / "
                            f"{int(progress_snapshot.get('remaining_count') or 0)}"
                        )
                        if item_label:
                            ui.label(f"{t('当前对象')}: {item_label}").classes("ag-subtle")
                        ui.label(f"{t('成功 / 失败 / 剩余')}: {counts_text}").classes("ag-subtle")
                        if message:
                            ui.label(message).classes("ag-subtle")

            ui.label(t("队列任务列表")).classes("ag-section-title q-mt-lg")
            with ui.element("div").classes("ag-ledger-list"):
                for index, job in enumerate(jobs, start=1):
                    runtime = runtimes.get(job.job_id)
                    state = runtime.state if runtime else QueueState.PENDING
                    source_mode = str((job.parameters or {}).get("source_mode") or "direct")
                    source_label = t("Zotero 报告模式") if source_mode == "zotero" else t("PDF 文件夹模式")
                    with ui.element("div").classes("ag-ledger-row"):
                        with ui.element("div").classes("ag-ledger-main"):
                            with ui.column().classes("gap-1"):
                                ui.label(f"#{index} · {controller.action_label(job.job_type)} · {job.project_name}").classes("ag-section-title")
                                with ui.element("div").classes("ag-ledger-meta"):
                                    ui.label(f"{t('输入来源')}: {source_label}")
                                    ui.label(f"ID: {job.job_id}")
                                    progress_snapshot = dict(getattr(runtime, "progress_snapshot", {}) or {}) if runtime else {}
                                    if progress_snapshot:
                                        message = str(progress_snapshot.get("message") or "")
                                        item_label = str(progress_snapshot.get("item_label") or "")
                                        counts_text = (
                                            f"{int(progress_snapshot.get('success_count') or 0)} / "
                                            f"{int(progress_snapshot.get('failure_count') or 0)} / "
                                            f"{int(progress_snapshot.get('remaining_count') or 0)}"
                                        )
                                        if item_label:
                                            ui.label(f"{t('当前对象')}: {item_label}")
                                        ui.label(f"{t('成功 / 失败 / 剩余')}: {counts_text}")
                                        if message:
                                            ui.label(message[:140] + ("..." if len(message) > 140 else ""))
                                    if runtime and runtime.error_message:
                                        ui.label(f"{t('错误信息')}: {runtime.error_message[:100]}...")
                            ui.label(state_labels.get(state, state.value)).classes("ag-build-badge")
                        with ui.row().classes("gap-2 flex-wrap"):
                            if state == QueueState.PENDING:
                                ui.button(
                                    t("上移"),
                                    on_click=lambda _event=None, jid=job.job_id: (controller.move_queue_job(jid, -1), refresh_queue_panel(False)),
                                ).props("outline size=sm")
                                ui.button(
                                    t("下移"),
                                    on_click=lambda _event=None, jid=job.job_id: (controller.move_queue_job(jid, 1), refresh_queue_panel(False)),
                                ).props("outline size=sm")
                            if state == QueueState.RUNNING:
                                ui.button(
                                    t("取消"),
                                    on_click=lambda _event=None, jid=job.job_id: (controller.cancel_job(jid), refresh_queue_panel(False)),
                                ).props("outline color=negative size=sm")
                            elif state in (QueueState.FAILED, QueueState.CANCELLED):
                                ui.button(
                                    t("重试"),
                                    on_click=lambda _event=None, jid=job.job_id: (controller.retry_job(jid), controller._schedule_queue_processor(), refresh_queue_panel(False)),
                                ).props("outline color=primary size=sm")
                            if state != QueueState.RUNNING:
                                ui.button(
                                    t("删除"),
                                    on_click=lambda _event=None, jid=job.job_id: (controller.remove_job(jid), refresh_queue_panel(False)),
                                ).props("outline color=negative size=sm")

    render_queue_panel()
    ui.timer(2.0, render_queue_panel.refresh)


def _render_workflow_input_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
        ui.label(t("任务起点")).classes("ag-section-title")
        ui.label(t("先选输入来源，再填写项目名。第一轮建议优先跑“仅分析文献”，确认摘要质量后再继续。")).classes("ag-subtle")
        with ui.grid(columns=2).classes("w-full gap-4 q-mt-md"):
            with ui.column().classes("gap-2"):
                ui.label(t("输入来源")).classes("ag-subtle")
                ui.toggle(
                    {"pdf": t("PDF 文件夹模式"), "zotero": t("Zotero 报告模式")},
                    value=controller.state["workflow"]["input_mode"],
                ).bind_value(controller.state["workflow"], "input_mode").classes("ag-mode-toggle ag-mode-toggle-2 w-full")
                with ui.element("div").classes("ag-toggle-ledger ag-toggle-ledger-2"):
                    with ui.element("div").classes("ag-toggle-note"):
                        ui.label(t("PDF 文件夹模式")).classes("text-body1")
                        ui.label(t("适合你已经把文献 PDF 放在一个文件夹里，想直接开始批量分析。")).classes("ag-subtle ag-wrap-note")
                    with ui.element("div").classes("ag-toggle-note"):
                        ui.label(t("Zotero 报告模式")).classes("text-body1")
                        ui.label(t("适合你已经有 Zotero report 和 library，希望沿着已有文献整理结果继续。")).classes("ag-subtle ag-wrap-note")
            with ui.column().classes("gap-2"):
                ui.input(t("项目名"), value=controller.state["workflow"]["project_name"]).bind_value(controller.state["workflow"], "project_name").classes("w-full")
                ui.checkbox(
                    t("Auto reuse historical stage-1 summaries"),
                    value=controller.state["workflow"]["reuse_stage1"],
                ).bind_value(controller.state["workflow"], "reuse_stage1")
                ui.label(
                    t("When enabled, stage 1 scans all historical project outputs plus compatible legacy summaries under the configured output path, then only analyzes the papers that are still missing.")
                ).classes("ag-subtle")

        with ui.column().classes("w-full gap-3 q-mt-md").bind_visibility_from(controller.state["workflow"], "input_mode", value="pdf"):
            _render_path_field(
                controller,
                label=t("PDF folder"),
                section="workflow",
                key="pdf_folder",
                pick="directory",
                title=t("Select PDF folder"),
            )
            with ui.row().classes("gap-2 flex-wrap"):
                ui.button(t("Open PDF folder"), on_click=lambda: _open_path(controller.state["workflow"]["pdf_folder"], controller.language)).props("outline")

        with ui.column().classes("w-full gap-3 q-mt-md").bind_visibility_from(controller.state["workflow"], "input_mode", value="zotero"):
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
            ui.label(t("Zotero 模式需要 report 和 library 两个路径；如果没配好，先去“环境与路径”页面补齐。")).classes("ag-subtle")
            with ui.row().classes("gap-2 flex-wrap"):
                ui.button(t("Open Zotero report"), on_click=lambda: _open_path(controller.state["paths"]["zotero_report"], controller.language)).props("outline")
                ui.button(t("打开 Zotero 库路径"), on_click=lambda: _open_path(controller.state["paths"]["library_path"], controller.language)).props("outline")
                ui.button(t("Open Setup"), on_click=lambda: ui.navigate.to("/setup")).props("outline")


def _render_workflow_mode_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full"):
        ui.label(t("运行方式")).classes("ag-section-title")
        ui.label(t("先用普通模式跑通第一轮；只有在确实需要额外概念抽取或先聊清写作意图时，再切换模式。")).classes("ag-subtle")
        ui.toggle(
            {
                "normal": t("普通模式"),
                "concept": t("概念增强模式"),
                "free": t("自由模式"),
            },
            value=controller.state["workflow"]["work_mode"],
        ).bind_value(controller.state["workflow"], "work_mode").classes("ag-mode-toggle ag-mode-toggle-3 w-full q-mt-md")
        with ui.element("div").classes("ag-toggle-ledger ag-toggle-ledger-3"):
            with ui.element("div").classes("ag-toggle-note"):
                ui.label(t("普通模式")).classes("text-body1")
                ui.label(t("普通模式：适合第一次运行和大多数常规任务。")).classes("ag-subtle ag-wrap-note")
            with ui.element("div").classes("ag-toggle-note"):
                ui.label(t("概念增强模式")).classes("text-body1")
                ui.label(t("概念增强：只在你要围绕某个核心概念补抓变量、定义和比较时使用。")).classes("ag-subtle ag-wrap-note")
            with ui.element("div").classes("ag-toggle-note"):
                ui.label(t("自由模式")).classes("text-body1")
                ui.label(t("自由模式：先和规划助手聊清目标，再把规划应用到本次任务。")).classes("ag-subtle ag-wrap-note")


def _render_workflow_concept_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full").bind_visibility_from(controller.state["workflow"], "work_mode", value="concept"):
        ui.label(t("概念增强（仅在概念模式下填写）")).classes("ag-section-title")
        ui.label(t("如果这次要围绕某个核心概念补抓变量、定义和比较关系，就填写概念词。普通模式可以留空。")).classes("ag-subtle")
        ui.input(
            t("概念增强模式概念词"),
            value=controller.state["workflow"]["concept"],
        ).bind_value(controller.state["workflow"], "concept").classes("w-full q-mt-md")


def _render_free_mode_planner_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card ag-card-strong p-6 w-full").bind_visibility_from(controller.state["workflow"], "work_mode", value="free"):
        ui.label(t("自由模式对话规划器")).classes("ag-section-title")
        ui.label(t("像聊天一样把综述想法发给规划助手；每发一轮，左侧会追加对话记录，右侧会刷新当前 prompt profile 草案。")).classes("ag-subtle")
        with ui.element("div").classes("ag-status-block q-mt-md"):
            status_label = ui.label("").classes("text-body1 ag-wrap-note")
            hint_label = ui.label("").classes("ag-subtle ag-wrap-note")
        with ui.element("div").classes("ag-planner-grid w-full q-mt-md"):
            transcript_view = ui.textarea(
                label=t("对话记录"),
                value="",
            ).props("outlined readonly rows=8").classes("w-full ag-planner-output")
            profile_view = ui.textarea(
                label=t("当前 profile 草案"),
                value="",
            ).props("outlined readonly rows=8").classes("w-full ag-planner-output")
        with ui.element("div").classes("ag-chat-composer q-mt-md"):
            with ui.element("div").classes("ag-chat-composer-head"):
                with ui.column().classes("gap-1"):
                    ui.label(t("在这里继续对话")).classes("ag-section-title")
                    ui.label(t("补充研究对象、概念关系、边界条件或你想强调的章节主线；发送后 profile 草案会随对话更新。")).classes("ag-subtle")
                ui.icon("forum").classes("text-xl")
            ui.textarea(
                label=t("输入下一轮回复"),
                value=controller.free_mode_chat_input,
                placeholder=t("例如：文件夹里主要有概念 A 和 B，我想写 A 如何推导到 B，重点比较理论解释、变量链路和 research gap。"),
            ).bind_value(controller, "free_mode_chat_input").props("outlined autogrow").classes("w-full ag-chat-input")
            with ui.row().classes("gap-2 flex-wrap"):
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
        ui.label(t("第一次使用建议按这个顺序：仅分析文献 → 生成大纲 → 生成全文。只有在流程稳定后，再使用一键运行。")).classes("ag-subtle")
        with ui.element("div").classes("ag-note-block q-mt-md"):
            ui.label(t("如果你是第一次跑这个项目，先点“仅分析文献”。如果已有可靠摘要或历史工作区，再继续点大纲、全文或验证。")).classes("ag-subtle")
        with ui.element("div").classes("ag-action-grid w-full q-mt-md"):
            for label_key, desc_key, action in action_specs:
                with ui.element("div").classes("ag-action-tile"):
                    ui.label(t(label_key)).classes("ag-section-title")
                    ui.label(t(desc_key)).classes("ag-subtle")
                    button_props = "outline"
                    if action == "analyze":
                        button_props = "unelevated color=primary"
                    elif action == "run_all":
                        button_props = "unelevated"
                    button = ui.button(
                        t(label_key),
                        on_click=lambda event=None, current_action=action: asyncio.create_task(controller.run_workflow(current_action)),
                    ).props(button_props).classes("w-full")
                    controller.register_action_button(button)


def _render_workflow_summary_reuse_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.expansion(t("高级：外部摘要文件（一般可跳过）"), icon="tune").classes("w-full"):
        with ui.card().classes("ag-card p-5 w-full q-mt-sm"):
            ui.label(t("历史阶段一摘要会自动扫描复用。这里只用于跳过阶段一、直接用已有 summaries.json 生成大纲/正文，或补充 output_path 之外的复用池。")).classes("ag-subtle")
            with ui.column().classes("gap-3 q-mt-md"):
                _render_path_field(
                    controller,
                    label=t("Use existing summaries.json for outline/review"),
                    section="workflow",
                    key="summary_file",
                    pick="file",
                    title=t("Select summaries.json file"),
                    filetypes=[("Summary Files", "*.json"), ("All Files", "*.*")],
                )
                ui.textarea(
                    t("Additional outline/review summary sources (one path per line)"),
                    value=controller.state["workflow"]["summary_sources"],
                ).bind_value(controller.state["workflow"], "summary_sources").props("outlined autogrow").classes("w-full")
                ui.textarea(
                    t("Extra stage-1 reuse pools outside output_path (one path per line)"),
                    value=controller.state["workflow"]["reuse_summary_files"],
                ).bind_value(controller.state["workflow"], "reuse_summary_files").props("outlined autogrow").classes("w-full")
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button(t("Open summary file"), on_click=lambda: _open_path(controller.state["workflow"]["summary_file"], controller.language)).props("outline")


def _render_workflow_recovery_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.expansion(t("补跑、恢复与验证（按需展开）"), icon="build_circle").classes("w-full"):
        with ui.card().classes("ag-card p-6 w-full q-mt-sm"):
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
                    ui.label(t("综述验证是可选增强步骤。需要额外核查时再运行，不影响默认主流程。")).classes("ag-subtle")
                    validate_button = ui.button(
                        t("验证综述"),
                        on_click=lambda: asyncio.create_task(controller.run_workflow("validate")),
                    ).props("outline").classes("w-full q-mt-md")
                    controller.register_action_button(validate_button)


def _render_workflow_checklist_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full"):
        ui.label(t("第一次运行建议")).classes("ag-section-title")
        ui.label(t("如果你是第一次使用，就按这条主路径走，不需要一次把所有高级能力都打开。")).classes("ag-subtle")
        with ui.column().classes("ag-checklist q-mt-md"):
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("先在“设置”里确认输出目录、输入路径和 API 模型都已连通。")).classes("ag-subtle")
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("再到这里选择输入来源：PDF 文件夹或 Zotero。")).classes("ag-subtle")
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("首次运行优先点击“仅分析文献”，确认摘要、预处理和提取质量。")).classes("ag-subtle")
            with ui.element("div").classes("ag-check-item"):
                ui.icon("check_circle").classes("text-base")
                ui.label(t("确认第一轮结果没问题后，再继续生成大纲、全文，或最后再使用一键运行。")).classes("ag-subtle")


def _render_workflow_navigation_card(controller: WorkspaceController) -> None:
    t = controller.t
    with ui.card().classes("ag-card p-6 w-full"):
        ui.label(t("相关入口")).classes("ag-section-title")
        ui.label(t("这里只保留工作台最常用的相关入口，减少工具按钮到处重复出现。")).classes("ag-subtle")
        with ui.column().classes("ag-button-column q-mt-md"):
            ui.button(t("前往设置"), on_click=lambda: ui.navigate.to("/setup")).props("outline").classes("w-full")
            ui.button(t("打开 API 页面"), on_click=lambda: ui.navigate.to("/setup/api")).props("outline").classes("w-full")
            ui.button(t("查看结果与日志"), on_click=lambda: ui.navigate.to("/logs")).props("outline").classes("w-full")
            ui.button(t("打开输出目录"), on_click=lambda: _open_path(controller.state["paths"]["output_path"], controller.language)).props("outline").classes("w-full")


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
        ui.label("auto-generate").classes("ag-chip q-mb-sm")
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
                    ui.label("auto-generate").classes("ag-section-title ag-topbar-title")
                    ui.label(controller.t(page_title)).classes("ag-build-badge")
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
            proxy_select = ui.select(
                {"environment": controller.t("跟随系统代理"), "direct": controller.t("直连")},
                value=card.get("proxy_mode", "environment"),
                label=controller.t("代理模式"),
            )
            proxy_select.bind_value(card, "proxy_mode")

        provider_select.on("update:model-value", lambda _: controller.preview_api_config(section_name))
        model_input.on("blur", lambda _: controller.preview_api_config(section_name))
        model_input.on("update:model-value", lambda _: controller.preview_api_config(section_name))
        api_base_input.on("blur", lambda _: controller.preview_api_config(section_name))
        api_base_input.on("update:model-value", lambda _: controller.preview_api_config(section_name))
        api_key_input.on("blur", lambda _: controller.preview_api_config(section_name))
        api_key_input.on("update:model-value", lambda _: controller.preview_api_config(section_name))
        proxy_select.on("update:model-value", lambda _: controller.preview_api_config(section_name))

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


def _render_mineru_api_card(controller: WorkspaceController) -> None:
    t = controller.t
    mineru = controller.state["mineru"]
    with ui.card().classes("ag-card ag-card-strong p-5 w-full"):
        ui.label(t("MinerU 远程解析")).classes("ag-section-title")
        ui.label(
            t("这是 PDF 预处理使用的远程解析后端，不属于 LLM 模型卡。是否真的调用，还取决于“性能与预处理”页里的解析策略。"),
        ).classes("ag-subtle")
        with ui.grid(columns=2).classes("w-full gap-3 q-mt-md"):
            ui.input("Base URL", value=mineru["base_url"]).bind_value(mineru, "base_url")
            token_input = ui.input(
                t("API Token"),
                value=mineru["api_token"],
                password=True,
                password_toggle_button=True,
            ).bind_value(mineru, "api_token")
            ui.input(t("模型版本"), value=mineru["model_version"]).bind_value(mineru, "model_version")
        with ui.element("div").classes("ag-note-block q-mt-md"):
            token_label = ui.label(controller._mineru_token_state_text()).classes("ag-subtle ag-wrap-note")
            runtime_label = ui.label(controller._mineru_runtime_state_text()).classes("ag-subtle ag-wrap-note q-mt-sm")

        def refresh_mineru_notes() -> None:
            token_label.set_text(controller._mineru_token_state_text())
            runtime_label.set_text(controller._mineru_runtime_state_text())

        token_input.on("update:model-value", lambda _: refresh_mineru_notes())
        token_input.on("blur", lambda _: refresh_mineru_notes())
        with ui.row().classes("gap-2 q-mt-sm"):
            ui.button(t("前往性能与预处理"), on_click=lambda: ui.navigate.to("/setup/processing")).props("outline")


def _render_processing_mineru_card(controller: WorkspaceController) -> None:
    t = controller.t
    preprocess = controller.state["preprocess"]
    mineru = controller.state["mineru"]
    parser_mode_options = {
        "local": t("local · 仅本地"),
        "hybrid": t("hybrid · 先本地后判定"),
        "remote_first": t("remote_first · 先尝试 MinerU"),
        "remote": t("remote · 只走 MinerU"),
    }
    parser_options = {
        "local": t("local · 本地解析链"),
        "mineru_remote": t("mineru_remote · MinerU 远程"),
    }
    with ui.card().classes("ag-card ag-card-strong p-6"):
        ui.label(t("解析策略与 MinerU")).classes("ag-section-title")
        ui.label(t("MinerU 会不会真正用上，取决于这里的 parser mode、主解析器和回退策略，而不只是有没有填 token。")).classes("ag-subtle")
        with ui.grid(columns=2).classes("w-full gap-3 q-mt-md"):
            parser_mode_select = ui.select(
                parser_mode_options,
                value=preprocess["parser_mode"],
                label=t("Parser mode"),
            ).bind_value(preprocess, "parser_mode")
            primary_parser_select = ui.select(
                parser_options,
                value=preprocess["primary_parser"],
                label=t("主解析器"),
            ).bind_value(preprocess, "primary_parser")
            fallback_parser_select = ui.select(
                parser_options,
                value=preprocess["fallback_parser"],
                label=t("回退解析器"),
            ).bind_value(preprocess, "fallback_parser")
            fallback_switch = ui.switch(
                t("允许本地回退"),
                value=mineru["allow_local_parse_fallback"],
            ).bind_value(mineru, "allow_local_parse_fallback")
        with ui.element("div").classes("ag-note-block q-mt-md"):
            token_label = ui.label(controller._mineru_token_state_text()).classes("ag-subtle ag-wrap-note")
            runtime_label = ui.label(controller._mineru_runtime_state_text()).classes("ag-subtle ag-wrap-note q-mt-sm")

        def refresh_mineru_notes() -> None:
            token_label.set_text(controller._mineru_token_state_text())
            runtime_label.set_text(controller._mineru_runtime_state_text())

        parser_mode_select.on("update:model-value", lambda _: refresh_mineru_notes())
        primary_parser_select.on("update:model-value", lambda _: refresh_mineru_notes())
        fallback_parser_select.on("update:model-value", lambda _: refresh_mineru_notes())
        fallback_switch.on("update:model-value", lambda _: refresh_mineru_notes())


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
        latest_snapshot = _latest_workspace_snapshot(
            controller.state["paths"]["output_path"],
            preferred_project=str(controller.state["workflow"]["project_name"]).strip(),
        )
        with _page_shell(
            controller,
            "总览",
            "这里先帮你建立一条清楚、安静的第一轮路径：先设置，再选输入来源和运行方式，最后进入工作台执行。",
            "/",
        ):
            with ui.card().classes("ag-card ag-card-strong ag-hero p-7 w-full"):
                with ui.grid(columns=2).classes("w-full items-stretch gap-6"):
                    with ui.column().classes("ag-card-stack"):
                        ui.label(t("本地网页工作台")).classes("ag-chip")
                        ui.label(t("先跑通第一轮，再逐步打开高级能力。")).classes("ag-section-title")
                        ui.label(
                            t("这个项目最适合用“研究工作台”的方式理解：先准备路径和模型，再进入工作台按输入来源、运行方式和主流程顺序推进。"),
                        ).classes("ag-subtle")
                        ui.label(t("如果你是第一次使用，不需要一开始就接触队列、自由模式或恢复操作。先把第一轮摘要跑稳最重要。")).classes("ag-subtle")
                        with ui.row().classes("ag-card-actions"):
                            ui.button(t("前往设置"), on_click=lambda: ui.navigate.to("/setup")).props("outline")
                            ui.button(t("进入工作台"), on_click=lambda: ui.navigate.to("/workflow")).props("unelevated color=primary")
                            ui.button(t("使用引导"), on_click=lambda: ui.navigate.to("/guide")).props("outline")
                    with ui.column().classes("ag-card-stack"):
                        with ui.element("div").classes("ag-note-block"):
                            ui.label(t("当前输出目录")).classes("ag-subtle")
                            ui.label(controller._compact_path_for_ui(controller.state["paths"]["output_path"] or "./output")).classes("text-body1 ag-wrap-note")
                        with ui.element("div").classes("ag-note-block"):
                            ui.label(t("最近日志数量")).classes("ag-subtle")
                            ui.label(str(_count_log_files())).classes("ag-kpi")
                        with ui.element("div").classes("ag-note-block"):
                            ui.label(t("解析策略")).classes("ag-subtle")
                            ui.label(str(controller.state["preprocess"]["parser_mode"] or "local")).classes("ag-kpi")
                        if latest_snapshot:
                            with ui.element("div").classes("ag-note-block"):
                                ui.label(t("最近一次任务")).classes("ag-subtle")
                                ui.label(f"{latest_snapshot['project_name']} · {latest_snapshot['status'] or '-'}").classes("text-body1")
                                ui.button(t("查看结果与日志"), on_click=lambda: ui.navigate.to("/logs")).props("outline size=sm").classes("q-mt-sm")

            with ui.element("div").classes("ag-grid-2 w-full"):
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("现在建议做什么")).classes("ag-section-title")
                    ui.label(t("总览页只保留方向和状态，不再重复输入来源和运行方式的长说明。")).classes("ag-subtle")
                    with ui.element("div").classes("ag-editorial-list q-mt-md"):
                        for step_index, title_key, note_key in [
                            ("01", "先完成基础设置", "先在设置页填好输出目录、输入路径和必要的模型配置。"),
                            ("02", "进入工作台选择输入来源", "工作台里只负责真正运行，不再把说明文字铺满整个页面。"),
                            ("03", "第一轮先跑仅分析文献", "先确认摘要、预处理和抽取质量，再继续大纲和全文。"),
                            ("04", "使用引导", "如果你需要完整的新手解释，再去“使用引导”页查看输入方式、运行方式和工作区说明。"),
                        ]:
                            with ui.element("div").classes("ag-editorial-step"):
                                ui.label(step_index).classes("ag-step-index")
                                with ui.column().classes("gap-1"):
                                    ui.label(t(title_key)).classes("ag-step-title")
                                    ui.label(t(note_key)).classes("ag-step-note")
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("当前工作台快照")).classes("ag-section-title")
                    ui.label(t("这里看当前配置状态；更完整的解释已经集中到“使用引导”页。")).classes("ag-subtle")
                    with ui.element("div").classes("ag-kv-grid q-mt-md"):
                        with ui.element("div").classes("ag-kv-item"):
                            ui.label(t("输出目录")).classes("ag-subtle")
                            ui.label(controller._compact_path_for_ui(controller.state["paths"]["output_path"] or "./output")).classes("ag-wrap-note")
                        with ui.element("div").classes("ag-kv-item"):
                            ui.label(t("解析策略")).classes("ag-subtle")
                            ui.label(str(controller.state["preprocess"]["parser_mode"] or "local"))
                        with ui.element("div").classes("ag-kv-item"):
                            ui.label(t("MinerU")).classes("ag-subtle")
                            ui.label(controller._mineru_token_state_text()).classes("ag-wrap-note")
                        with ui.element("div").classes("ag-kv-item"):
                            ui.label(t("最近日志数量")).classes("ag-subtle")
                            ui.label(str(_count_log_files()))
                    ui.label(controller._mineru_runtime_state_text()).classes("ag-subtle ag-wrap-note q-mt-md")

            with ui.card().classes("ag-card p-6 w-full"):
                ui.label(t("常用入口")).classes("ag-section-title")
                ui.label(t("这里只保留最常回访的页面；第一次使用的完整解释请看“使用引导”。")).classes("ag-subtle")
                with ui.element("div").classes("ag-grid-compact q-mt-md"):
                    with ui.element("div").classes("ag-mini-card"):
                        ui.label(t("环境与路径")).classes("ag-section-title")
                        ui.label(t("先把输出目录、Zotero 路径和基础 setup 定下来。")).classes("ag-subtle ag-wrap-note")
                        ui.button(t("前往设置"), on_click=lambda: ui.navigate.to("/setup")).props("outline").classes("q-mt-md")
                    with ui.element("div").classes("ag-mini-card"):
                        ui.label(t("工作台")).classes("ag-section-title")
                        ui.label(t("真正的输入来源、运行方式和主流程按钮都在这里。")).classes("ag-subtle ag-wrap-note")
                        ui.button(t("进入工作台"), on_click=lambda: ui.navigate.to("/workflow")).props("unelevated color=primary").classes("q-mt-md")
                    with ui.element("div").classes("ag-mini-card"):
                        ui.label(t("结果与日志")).classes("ag-section-title")
                        ui.label(t("最近一次 job workspace、主要产物和日志入口都集中在这里。")).classes("ag-subtle ag-wrap-note")
                        ui.button(t("查看结果与日志"), on_click=lambda: ui.navigate.to("/logs")).props("outline").classes("q-mt-md")
                    with ui.element("div").classes("ag-mini-card"):
                        ui.label(t("使用引导")).classes("ag-section-title")
                        ui.label(t("输入方式、运行策略、OCR / MinerU、复用和工作区的完整说明都在这一页。")).classes("ag-subtle ag-wrap-note")
                        ui.button(t("使用引导"), on_click=lambda: ui.navigate.to("/guide")).props("outline").classes("q-mt-md")

    @ui.page("/workflow")
    def workflow_page() -> None:
        with _page_shell(
            controller,
            "工作台",
            "这里按“输入来源 → 运行方式 → 主流程”的顺序组织。高级复用、补跑和验证都放在次级区域，避免第一次使用被打断。",
            "/workflow",
        ):
            with ui.element("div").classes("ag-workflow-shell w-full"):
                with ui.column().classes("gap-5 w-full"):
                    _render_workflow_input_card(controller)
                    _render_workflow_mode_card(controller)
                    _render_workflow_concept_card(controller)
                    _render_free_mode_planner_card(controller)
                    _render_workflow_actions_card(controller)
                    _render_workflow_summary_reuse_card(controller)
                    _render_workflow_recovery_card(controller)
                with ui.column().classes("ag-sidebar-stack w-full"):
                    _render_progress_card(controller)
                    _render_workflow_queue_card(controller)
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
                    ui.label(t("5. MinerU token 在“API 与模型”页填写；真正是否调用，要到“性能与预处理”页选择解析策略。")).classes("ag-subtle")
                    with ui.row().classes("gap-2 q-mt-md"):
                        ui.button(t("前往 API 与模型"), on_click=lambda: ui.navigate.to("/setup/api")).props("outline")
                        ui.button(t("前往性能与预处理"), on_click=lambda: ui.navigate.to("/setup/processing")).props("outline")

    @ui.page("/setup/api")
    def api_page() -> None:
        with _page_shell(
            controller,
            "API 与模型",
            "阅读 / 写作 / 大纲 / 自由模式 / 验证模型都在这里配置；MinerU 远程解析的 token 也放在这里统一管理。",
            "/setup/api",
        ):
            with ui.column().classes("w-full gap-4"):
                _render_api_card(controller, "Primary_Reader_API", "阅读模型", "优先负责文献分析与阶段一抽取。")
                _render_api_card(controller, "Backup_Reader_API", "备用阅读模型", "当主阅读模型失败或限流时，系统可以兜底。")
                _render_api_card(controller, "Writer_API", "写作模型", "负责大段综述写作与章节生成。")
                _render_api_card(controller, "Outline_API", "大纲模型", "优先负责框架大纲规划；未配置时可回退到写作模型。")
                _render_api_card(controller, "Free_Mode_API", "自由模式对话模型", "优先负责自由模式前置对话规划；未配置时可回退到大纲模型。")
                _render_api_card(controller, "Validator_API", "验证模型", "用于综述校验和质量复查。")
                _render_mineru_api_card(controller)

    @ui.page("/setup/processing")
    def processing_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "性能与预处理",
            "这一页专门控制并发、解析策略、PDF 预处理、OCR 和本地 RAG。MinerU 是否真正启用，也在这里决定。",
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

                _render_processing_mineru_card(controller)

                with ui.card().classes("ag-card p-6"):
                    ui.label(t("高级 / 可选功能")).classes("ag-section-title")
                    ui.label(t("这里只保留仍然建议用户直接控制的高级项。综述验证是可选增强步骤，默认不改变主流程。")).classes("ag-subtle")
                    with ui.expansion(t("高级 / 可选功能"), icon="science").classes("w-full q-mt-md"):
                        with ui.column().classes("gap-2 q-pa-sm"):
                            ui.switch(t("启用综述验证"), value=controller.state["performance"]["enable_stage2_validation"]).bind_value(controller.state["performance"], "enable_stage2_validation")

    @ui.page("/logs")
    def logs_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "结果与日志",
            "优先查看最近一次任务的工作区和主要产物；日志是辅助线索，不再是唯一入口。",
            "/logs",
        ):
            _render_progress_card(controller)

            def refresh_results_page() -> None:
                controller.refresh_logs()
                render_latest_workspace.refresh()
                render_log_excerpt.refresh()

            @ui.refreshable
            def render_latest_workspace() -> None:
                preferred_project = str(controller.state["workflow"]["project_name"]).strip()
                latest_snapshot = _latest_workspace_snapshot(
                    controller.state["paths"]["output_path"],
                    preferred_project=preferred_project,
                )
                with ui.card().classes("ag-card ag-card-strong p-6"):
                    ui.label(t("最近一次任务")).classes("ag-section-title")
                    if not latest_snapshot:
                        ui.label(t("当前还没有可识别的任务工作区。先去工作台运行一次任务。")).classes("ag-subtle q-mt-sm")
                        with ui.row().classes("gap-2 q-mt-md"):
                            ui.button(t("进入工作台"), on_click=lambda: ui.navigate.to("/workflow")).props("unelevated")
                            ui.button(t("前往设置"), on_click=lambda: ui.navigate.to("/setup")).props("outline")
                        return

                    with ui.element("div").classes("ag-summary-strip q-mt-md"):
                        for label_key, value in [
                            ("项目", latest_snapshot["project_name"] or "-"),
                            ("任务 ID", latest_snapshot["job_id"] or "-"),
                            ("工作区状态", latest_snapshot["status"] or "-"),
                            ("更新时间", latest_snapshot["updated_at"] or "-"),
                        ]:
                            with ui.element("div").classes("ag-summary-item"):
                                ui.label(t(label_key)).classes("ag-subtle")
                                ui.label(str(value)).classes("text-body1")

                    with ui.element("div").classes("ag-note-block q-mt-md"):
                        ui.label(t("工作区路径")).classes("ag-subtle")
                        ui.label(str(latest_snapshot["workspace_path"])).classes("text-body1")

                    with ui.row().classes("gap-2 q-mt-md flex-wrap"):
                        ui.button(
                            t("打开工作区"),
                            on_click=lambda _event, path=latest_snapshot["workspace_path"]: _open_path(
                                path, controller.language
                            ),
                        ).props("unelevated")
                        ui.button(
                            t("打开产物目录"),
                            on_click=lambda _event, path=str(
                                Path(latest_snapshot["workspace_path"]) / "artifacts"
                            ): _open_path(path, controller.language),
                        ).props("outline")
                        ui.button(
                            t("打开报告目录"),
                            on_click=lambda _event, path=str(
                                Path(latest_snapshot["workspace_path"]) / "reports"
                            ): _open_path(path, controller.language),
                        ).props("outline")
                        ui.button(
                            t("打开注册表"),
                            on_click=lambda _event, path=latest_snapshot[
                                "artifact_registry_path"
                            ]: _open_path(path, controller.language),
                        ).props("outline")

                    ui.label(t("主要产物")).classes("ag-subtle q-mt-md")
                    if latest_snapshot["artifacts"]:
                        with ui.row().classes("gap-2 q-mt-sm flex-wrap"):
                            for label_key, path in latest_snapshot["artifacts"]:
                                ui.button(t(label_key), on_click=lambda _event=None, target=path: _open_path(target, controller.language)).props("outline size=sm")
                    else:
                        ui.label(t("目前还没有检出的主要产物。")).classes("ag-subtle q-mt-sm")

            @ui.refreshable
            def render_log_excerpt() -> None:
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("最近日志文件")).classes("ag-section-title")
                    ui.label(controller.latest_log_path or t("暂无日志文件。")).classes("ag-subtle q-mt-sm")
                    with ui.row().classes("gap-2 q-mt-md flex-wrap"):
                        ui.button(t("刷新日志"), on_click=refresh_results_page).props("unelevated")
                        ui.button(t("打开日志目录"), on_click=lambda: _open_path(str(REPO_ROOT / "logs"), controller.language)).props("outline")
                        ui.button(t("打开输出目录"), on_click=lambda: _open_path(controller.state["paths"]["output_path"], controller.language)).props("outline")
                    ui.textarea(value=controller.latest_log_excerpt).props("outlined readonly autogrow").classes("w-full q-mt-md")

            with ui.element("div").classes("ag-grid-2 w-full"):
                render_latest_workspace()
                render_log_excerpt()
            if not controller.test_mode:
                ui.timer(1.2, controller.refresh_progress)
                ui.timer(2.0, refresh_results_page)

    @ui.page("/queue")
    def queue_page() -> None:
        # Compatibility route for saved URLs. Queue creation and management now live on /workflow.
        ui.navigate.to("/workflow")

    @ui.page("/guide")
    def guide_page() -> None:
        t = controller.t
        with _page_shell(
            controller,
            "使用引导",
            "这页保留第一次使用所需的完整说明：输入来源、运行方式、OCR / MinerU、复用和工作区应该怎么理解。",
            "/guide",
        ):
            with ui.card().classes("ag-card ag-card-strong p-6 w-full"):
                ui.label(t("第一次运行，只看这一页也能开始")).classes("ag-section-title")
                ui.label(t("下面这五步对应 GUI 里最重要的页面和动作。先跑通，再回头用高级功能。")).classes("ag-subtle")
                with ui.element("div").classes("ag-editorial-list q-mt-md"):
                    for step_index, title_key, note_key in [
                        ("01", "准备输入材料", "PDF 模式只需要文件夹；Zotero 模式需要 report 和 library。"),
                        ("02", "完成设置与模型连接", "先去设置页填路径，再检查 Reader / Writer / Outline 等模型是否可用。"),
                        ("03", "进入工作台选择运行方式", "普通模式最适合第一次跑；概念增强和自由模式只在有明确需要时再用。"),
                        ("04", "先跑仅分析文献", "先确认结构化摘要、预处理和抽取质量，再决定是否继续大纲和全文。"),
                        ("05", "去结果与日志页看工作区", "最新 job workspace 和主要产物比原始日志更值得先看。"),
                    ]:
                        with ui.element("div").classes("ag-editorial-step"):
                            ui.label(step_index).classes("ag-step-index")
                            with ui.column().classes("gap-1"):
                                ui.label(t(title_key)).classes("ag-step-title")
                                ui.label(t(note_key)).classes("ag-step-note")

            with ui.element("div").classes("ag-grid-2 w-full"):
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("输入方式说明")).classes("ag-section-title")
                    with ui.element("div").classes("ag-editorial-list q-mt-md"):
                        with ui.element("div").classes("ag-editorial-step"):
                            ui.label("PDF").classes("ag-step-index")
                            with ui.column().classes("gap-1"):
                                ui.label(t("PDF 文件夹模式")).classes("ag-step-title")
                                ui.label(t("适合你已经准备好 PDF 文件夹，想直接开始批量分析。")).classes("ag-step-note")
                        with ui.element("div").classes("ag-editorial-step"):
                            ui.label("ZT").classes("ag-step-index")
                            with ui.column().classes("gap-1"):
                                ui.label(t("Zotero 报告模式")).classes("ag-step-title")
                                ui.label(t("适合你已经整理好 Zotero report 和文献库，希望沿着现有整理结果继续。")).classes("ag-step-note")
                with ui.card().classes("ag-card p-6"):
                    ui.label(t("运行方式说明")).classes("ag-section-title")
                    with ui.element("div").classes("ag-editorial-list q-mt-md"):
                        with ui.element("div").classes("ag-editorial-step"):
                            ui.label("01").classes("ag-step-index")
                            with ui.column().classes("gap-1"):
                                ui.label(t("普通模式")).classes("ag-step-title")
                                ui.label(t("最稳妥，最适合第一轮。")).classes("ag-step-note")
                        with ui.element("div").classes("ag-editorial-step"):
                            ui.label("02").classes("ag-step-index")
                            with ui.column().classes("gap-1"):
                                ui.label(t("概念增强模式")).classes("ag-step-title")
                                ui.label(t("适合围绕某个概念做更聚焦的抽取、定义和比较。")).classes("ag-step-note")
                        with ui.element("div").classes("ag-editorial-step"):
                            ui.label("03").classes("ag-step-index")
                            with ui.column().classes("gap-1"):
                                ui.label(t("自由模式")).classes("ag-step-title")
                                ui.label(t("适合先和规划助手聊清楚目标，再把当前规划应用到本次任务。")).classes("ag-step-note")

            with ui.card().classes("ag-card p-6 w-full"):
                ui.label(t("关于 OCR、MinerU、复用和工作区")).classes("ag-section-title")
                ui.label(t("默认不是全量 OCR。系统会先判断 PDF 是否有可用文本，再只对异常页触发 OCR。这样更省性能，也更适合普通电脑。")).classes("ag-subtle")
                ui.label(t("MinerU 也不是默认常开：只有 parser mode 请求远程，且 hybrid 判定本地质量不足时，才会真正发起远程解析。")).classes("ag-subtle q-mt-sm")
                ui.label(t("阶段一复用开启后，会自动扫描历史输出并尽量跳过已经分析过的论文。")).classes("ag-subtle q-mt-sm")
                ui.label(t("大多数真实产物现在都写入 output/<project_name>__<job_id>/ 工作区；旧的 output/<project_name>/ 更像兼容指针目录。")).classes("ag-subtle q-mt-sm")

            with ui.card().classes("ag-card p-6 w-full"):
                ui.label(t("后台队列怎么用")).classes("ag-section-title")
                ui.label(t("队列不是单独入口。它接住工作台提交的主流程任务，让任务在后台一个接一个跑，避免多个长任务互相抢资源。")).classes("ag-subtle")
                with ui.element("div").classes("ag-editorial-list q-mt-md"):
                    for step_index, title_key, note_key in [
                        ("01", "先在工作台配好当前任务", "选择输入来源、项目名、运行方式和必要路径。"),
                        ("02", "点击主流程按钮提交", "点击“仅分析文献 / 生成大纲 / 生成全文 / 一键运行”后，任务会自动入队。"),
                        ("03", "继续准备下一项", "任务入队后表单仍可编辑，你可以继续配置下一个项目。"),
                        ("04", "在队列里处理异常", "待处理任务可调整顺序，失败任务可以重试，完成任务可以清空。"),
                    ]:
                        with ui.element("div").classes("ag-editorial-step"):
                            ui.label(step_index).classes("ag-step-index")
                            with ui.column().classes("gap-1"):
                                ui.label(t(title_key)).classes("ag-step-title")
                                ui.label(t(note_key)).classes("ag-step-note")

    ui.run(
        host="127.0.0.1",
        port=port,
        title="auto-generate",
        reload=reload,
        show=show,
    )
