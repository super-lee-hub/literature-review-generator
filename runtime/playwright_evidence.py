"""Real localhost GUI/Playwright evidence collection for Gate I.

This module intentionally has no fallback that turns a filename or a hand
written status JSON into browser evidence.  A successful result requires a
running local GUI, a real Playwright trace archive, typed run metadata, and a
bound resulting runtime job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Mapping
import zipfile

from services.job_workspace import atomic_write_json


class PlaywrightEvidenceError(RuntimeError):
    """Raised when the real Gate I action cannot produce valid evidence."""


_INPUT_FIELDS = frozenset(
    {
        "artifact_type",
        "artifact_version",
        "schema_version",
        "base_url",
        "config_path",
        "workspace",
        "resulting_job_id",
        "repo_root",
        "port",
        "startup_timeout_seconds",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PlaywrightScenarioInputV1:
    base_url: str
    config_path: str
    workspace: str
    resulting_job_id: str
    repo_root: str
    port: int
    startup_timeout_seconds: float = 30.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PlaywrightScenarioInputV1":
        if not isinstance(payload, Mapping):
            raise PlaywrightEvidenceError("Playwright scenario input must be a JSON object")
        unknown = sorted(str(key) for key in payload if str(key) not in _INPUT_FIELDS)
        if unknown:
            raise PlaywrightEvidenceError(
                "Playwright scenario input contains unknown fields: " + ", ".join(unknown)
            )
        if payload.get("artifact_type") != "acceptance_gui_input":
            raise PlaywrightEvidenceError("Playwright scenario input artifact type is invalid")
        if payload.get("artifact_version") != "v1" or payload.get("schema_version") != "acceptance-gui-input-v1":
            raise PlaywrightEvidenceError("Playwright scenario input schema is invalid")
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url.startswith("http://"):
            raise PlaywrightEvidenceError("Playwright scenario input must use an http localhost URL")
        try:
            from urllib.parse import urlsplit

            parsed = urlsplit(base_url)
        except ValueError as exc:
            raise PlaywrightEvidenceError("Playwright scenario input URL is invalid") from exc
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password:
            raise PlaywrightEvidenceError("Playwright scenario input URL must target localhost")
        config_path = str(payload.get("config_path") or "").strip()
        workspace = str(payload.get("workspace") or "").strip()
        resulting_job_id = str(payload.get("resulting_job_id") or "").strip()
        repo_root = str(payload.get("repo_root") or "").strip()
        if not config_path:
            raise PlaywrightEvidenceError("Playwright scenario input config_path is required")
        if not workspace:
            raise PlaywrightEvidenceError("Playwright scenario input workspace is required")
        if not resulting_job_id:
            raise PlaywrightEvidenceError("Playwright scenario input resulting_job_id is required")
        if not repo_root:
            raise PlaywrightEvidenceError("Playwright scenario input repo_root is required")
        raw_port = payload.get("port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int) or not (1 <= raw_port <= 65535):
            raise PlaywrightEvidenceError("Playwright scenario input port is invalid")
        try:
            timeout = float(payload.get("startup_timeout_seconds", 30.0))
        except (TypeError, ValueError) as exc:
            raise PlaywrightEvidenceError("Playwright scenario input startup timeout is invalid") from exc
        if timeout <= 0:
            raise PlaywrightEvidenceError("Playwright scenario input startup timeout must be positive")
        return cls(
            base_url=base_url,
            config_path=config_path,
            workspace=workspace,
            resulting_job_id=resulting_job_id,
            repo_root=repo_root,
            port=raw_port,
            startup_timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class PlaywrightEvidenceResultV1:
    browser_evidence_path: Path
    trace_path: Path
    screenshot_manifest_path: Path

    def paths(self) -> tuple[Path, Path, Path]:
        return (self.browser_evidence_path, self.trace_path, self.screenshot_manifest_path)


class PlaywrightEvidenceCollector:
    """Start the real GUI and run the documented smoke flow."""

    def __init__(
        self,
        scenario_input: PlaywrightScenarioInputV1,
        *,
        acceptance_run_id: str,
        scenario_id: str,
        final_executable_sha: str,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.input = scenario_input
        self.acceptance_run_id = str(acceptance_run_id).strip()
        self.scenario_id = str(scenario_id).strip()
        self.final_executable_sha = str(final_executable_sha).strip()
        self.environment = dict(environment or os.environ)
        if not self.acceptance_run_id or self.scenario_id != "I" or not self.final_executable_sha:
            raise PlaywrightEvidenceError("Playwright collector is missing acceptance identity")

    def run(self) -> PlaywrightEvidenceResultV1:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PlaywrightEvidenceError("Playwright runtime is not installed") from exc

        config_path = Path(self.input.config_path).expanduser().resolve()
        workspace = Path(self.input.workspace).expanduser().resolve()
        repo_root = Path(self.input.repo_root).expanduser().resolve()
        if not config_path.is_file() or config_path.is_symlink():
            raise PlaywrightEvidenceError("Playwright GUI config is missing or unsafe")
        if not repo_root.is_dir() or repo_root.is_symlink():
            raise PlaywrightEvidenceError("Playwright GUI repo root is missing or unsafe")
        workspace.mkdir(parents=True, exist_ok=True)
        evidence_root = workspace / "acceptance_gui"
        evidence_root.mkdir(parents=True, exist_ok=True)
        trace_path = evidence_root / "trace.zip"
        browser_path = evidence_root / "playwright_run_evidence.json"
        screenshot_path = evidence_root / "dashboard.png"
        screenshot_manifest_path = evidence_root / "screenshot_manifest.json"
        process_env = dict(self.environment)
        process_env["AUTO_GENERATE_GUI_TEST_MODE"] = "1"
        process_env["NICEGUI_SCREEN_TEST_PORT"] = str(self.input.port)
        gui_process = subprocess.Popen(
            [
                sys.executable,
                "launch_gui.py",
                "--no-show",
                "--port",
                str(self.input.port),
                "--config",
                str(config_path),
            ],
            cwd=str(repo_root),
            env=process_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started_at = _now()
        session_id = f"{self.acceptance_run_id}:I:{gui_process.pid}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        assertions: list[dict[str, Any]] = []
        screenshots: list[dict[str, Any]] = []
        trace_stopped = False
        try:
            self._wait_for_server(gui_process)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                try:
                    page.goto(self.input.base_url, wait_until="domcontentloaded")
                    self._assert_visible(page, ".ag-fixedbar-shell", "dashboard_shell_visible", assertions)
                    self._assert_text(page, ".ag-topbar-title", "auto-generate", "dashboard_title", assertions)
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    screenshots.append({"name": "dashboard", "path": str(screenshot_path)})
                    button = page.get_by_role("button", name="进入工作台").first
                    if button.count() > 0:
                        button.click()
                        self._assert_url_suffix(page, "/workflow", "workflow_navigation", assertions)
                    else:
                        assertions.append({"name": "workflow_navigation", "passed": False, "detail": "documented button missing"})
                finally:
                    page.close()
                    context.tracing.stop(path=str(trace_path))
                    trace_stopped = True
                    context.close()
                    browser.close()
            if not trace_path.is_file() or not zipfile.is_zipfile(trace_path):
                raise PlaywrightEvidenceError("Playwright did not produce a valid trace archive")
            trace_sha = _sha256(trace_path)
            atomic_write_json(
                str(screenshot_manifest_path),
                {
                    "artifact_type": "playwright_screenshot_manifest",
                    "artifact_version": "v1",
                    "schema_version": "playwright-screenshot-manifest-v1",
                    "acceptance_run_id": self.acceptance_run_id,
                    "scenario_id": self.scenario_id,
                    "screenshots": screenshots,
                },
            )
            atomic_write_json(
                str(browser_path),
                {
                    "artifact_type": "playwright_run_evidence",
                    "artifact_version": "v1",
                    "schema_version": "playwright-run-evidence-v1",
                    "run_id": self.acceptance_run_id,
                    "session_id": session_id,
                    "url": self.input.base_url,
                    "resulting_job_id": self.input.resulting_job_id,
                    "trace_sha256": trace_sha,
                    "flow_assertions": assertions,
                    "console_errors": console_errors,
                    "page_errors": page_errors,
                    "started_at": started_at,
                    "completed_at": _now(),
                    "gui_pid": gui_process.pid,
                    "screenshot_manifest_path": str(screenshot_manifest_path),
                },
            )
            if any(item.get("passed") is not True for item in assertions) or console_errors or page_errors:
                raise PlaywrightEvidenceError("documented Playwright flow did not complete cleanly")
            return PlaywrightEvidenceResultV1(
                browser_evidence_path=browser_path,
                trace_path=trace_path,
                screenshot_manifest_path=screenshot_manifest_path,
            )
        except BaseException:
            if trace_path.is_file() and not trace_stopped:
                # There is no valid execution receipt on an incomplete trace.
                try:
                    trace_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            if gui_process.poll() is None:
                gui_process.terminate()
                try:
                    gui_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    gui_process.kill()
                    gui_process.wait(timeout=10)

    def _wait_for_server(self, process: subprocess.Popen[str]) -> None:
        from urllib.request import urlopen

        deadline = time.time() + self.input.startup_timeout_seconds
        while time.time() < deadline:
            if process.poll() is not None:
                raise PlaywrightEvidenceError(
                    f"GUI process exited before readiness: {process.returncode}"
                )
            try:
                with urlopen(self.input.base_url, timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.25)
        raise PlaywrightEvidenceError("GUI server did not become ready before timeout")

    @staticmethod
    def _assert_visible(page: Any, selector: str, name: str, assertions: list[dict[str, Any]]) -> None:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            assertions.append({"name": name, "passed": False, "detail": str(exc)})
            raise
        assertions.append({"name": name, "passed": True})

    @staticmethod
    def _assert_text(page: Any, selector: str, expected: str, name: str, assertions: list[dict[str, Any]]) -> None:
        try:
            actual = page.locator(selector).first.inner_text()
            if expected not in actual:
                raise AssertionError(f"expected {expected!r} in {actual!r}")
        except Exception as exc:
            assertions.append({"name": name, "passed": False, "detail": str(exc)})
            raise
        assertions.append({"name": name, "passed": True})

    @staticmethod
    def _assert_url(page: Any, suffix: str, name: str, assertions: list[dict[str, Any]]) -> None:
        try:
            if not str(page.url).endswith(suffix):
                raise AssertionError(f"expected URL suffix {suffix!r}, got {page.url!r}")
        except Exception as exc:
            assertions.append({"name": name, "passed": False, "detail": str(exc)})
            raise
        assertions.append({"name": name, "passed": True})

    @classmethod
    def _assert_url_suffix(cls, page: Any, suffix: str, name: str, assertions: list[dict[str, Any]]) -> None:
        cls._assert_url(page, suffix, name, assertions)


__all__ = [
    "PlaywrightEvidenceCollector",
    "PlaywrightEvidenceError",
    "PlaywrightEvidenceResultV1",
    "PlaywrightScenarioInputV1",
]
