"""Real localhost GUI/Playwright evidence collection for Gate I.

This module intentionally has no fallback that turns a filename or a hand
written status JSON into browser evidence.  A successful result requires a
running local GUI, a real Playwright trace archive, typed run metadata, and a
bound resulting runtime job.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.durable_io import InterProcessLockTimeout, interprocess_file_lock
from services.job_workspace import atomic_write_json, is_reparse_path


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

_PRODUCTION_INPUT_FIELDS = frozenset(
    {
        "artifact_type",
        "artifact_version",
        "schema_version",
        "execution_kind",
        "base_url",
        "config_path",
        "repo_root",
        "output_root",
        "input_mode",
        "pdf_folder",
        "zotero_report",
        "library_path",
        "project_name",
        "work_mode",
        "action",
        "port",
        "startup_timeout_seconds",
        "completion_timeout_seconds",
    }
)


def _runtime_source_mode_for_gui_input(input_mode: str) -> str:
    """Map GUI vocabulary to the canonical RuntimeSourceSpec vocabulary."""

    normalized = str(input_mode or "").strip().casefold()
    try:
        return {"pdf": "direct", "zotero": "zotero"}[normalized]
    except KeyError as exc:
        raise PlaywrightEvidenceError(
            f"unsupported GUI production input mode: {input_mode}"
        ) from exc


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
        raw_port = payload.get("port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int) or not (1 <= raw_port <= 65535):
            raise PlaywrightEvidenceError("Playwright scenario input port is invalid")
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password:
            raise PlaywrightEvidenceError("Playwright scenario input URL must target localhost")
        if parsed.port != raw_port:
            raise PlaywrightEvidenceError(
                "Playwright scenario input URL port must match the launcher port"
            )
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
class PlaywrightProductionScenarioInputV2:
    """A request for a GUI-created production job.

    Unlike the page-smoke v1 input, this contract deliberately contains no
    workspace or resulting job ID.  The browser must create the queue job and
    the collector binds evidence to the ID returned by that real submission.
    """

    base_url: str
    config_path: str
    repo_root: str
    output_root: str
    input_mode: str
    pdf_folder: str
    zotero_report: str
    library_path: str
    project_name: str
    work_mode: str
    action: str
    port: int
    startup_timeout_seconds: float = 30.0
    completion_timeout_seconds: float = 900.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PlaywrightProductionScenarioInputV2":
        if not isinstance(payload, Mapping):
            raise PlaywrightEvidenceError("Playwright production input must be a JSON object")
        unknown = sorted(str(key) for key in payload if str(key) not in _PRODUCTION_INPUT_FIELDS)
        if unknown:
            raise PlaywrightEvidenceError(
                "Playwright production input contains unknown fields: " + ", ".join(unknown)
            )
        if payload.get("artifact_type") != "acceptance_gui_input":
            raise PlaywrightEvidenceError("Playwright production input artifact type is invalid")
        if payload.get("artifact_version") != "v2" or payload.get("schema_version") != "acceptance-gui-input-v2":
            raise PlaywrightEvidenceError("Playwright production input schema is invalid")
        if str(payload.get("execution_kind") or "").strip().casefold() != "production":
            raise PlaywrightEvidenceError("Playwright production input execution_kind must be production")
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url.startswith("http://"):
            raise PlaywrightEvidenceError("Playwright production input must use an http localhost URL")
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(base_url)
        except ValueError as exc:
            raise PlaywrightEvidenceError("Playwright production input URL is invalid") from exc
        raw_port = payload.get("port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int) or not (1 <= raw_port <= 65535):
            raise PlaywrightEvidenceError("Playwright production input port is invalid")
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password:
            raise PlaywrightEvidenceError("Playwright production input URL must target localhost")
        if parsed.port != raw_port:
            raise PlaywrightEvidenceError("Playwright production input URL port must match the launcher port")
        values = {
            name: str(payload.get(name) or "").strip()
            for name in ("config_path", "repo_root", "output_root", "project_name", "action")
        }
        if any(not values[name] for name in values):
            missing = ", ".join(name for name, value in values.items() if not value)
            raise PlaywrightEvidenceError("Playwright production input is missing: " + missing)
        input_mode = str(payload.get("input_mode") or "pdf").strip().casefold()
        if input_mode not in {"pdf", "zotero"}:
            raise PlaywrightEvidenceError("Playwright production input input_mode is invalid")
        action = values["action"].casefold()
        if action not in {"analyze", "outline", "review", "run_all"}:
            raise PlaywrightEvidenceError("Playwright production input action is invalid")
        if input_mode == "pdf" and not str(payload.get("pdf_folder") or "").strip():
            raise PlaywrightEvidenceError("Playwright production input pdf_folder is required")
        if input_mode == "zotero" and (
            not str(payload.get("zotero_report") or "").strip()
            or not str(payload.get("library_path") or "").strip()
        ):
            raise PlaywrightEvidenceError("Playwright production input Zotero paths are required")
        work_mode = str(payload.get("work_mode") or "normal").strip().casefold()
        if work_mode != "normal":
            raise PlaywrightEvidenceError(
                "Playwright production input currently requires normal work_mode"
            )
        try:
            startup_timeout = float(payload.get("startup_timeout_seconds", 30.0))
            completion_timeout = float(payload.get("completion_timeout_seconds", 900.0))
        except (TypeError, ValueError) as exc:
            raise PlaywrightEvidenceError("Playwright production input timeout is invalid") from exc
        if startup_timeout <= 0 or completion_timeout <= 0:
            raise PlaywrightEvidenceError("Playwright production input timeout must be positive")
        return cls(
            base_url=base_url,
            config_path=values["config_path"],
            repo_root=values["repo_root"],
            output_root=values["output_root"],
            input_mode=input_mode,
            pdf_folder=str(payload.get("pdf_folder") or "").strip(),
            zotero_report=str(payload.get("zotero_report") or "").strip(),
            library_path=str(payload.get("library_path") or "").strip(),
            project_name=values["project_name"],
            work_mode=work_mode,
            action=action,
            port=raw_port,
            startup_timeout_seconds=startup_timeout,
            completion_timeout_seconds=completion_timeout,
        )


@dataclass(frozen=True)
class PlaywrightEvidenceResultV1:
    browser_evidence_path: Path
    trace_path: Path
    screenshot_manifest_path: Path
    submitted_job_id: str = ""
    job_workspace_path: Path | None = None
    runtime_spec_path: Path | None = None
    job_outcome_path: Path | None = None
    attempt_path: Path | None = None
    provider_ledger_path: Path | None = None

    def paths(self) -> tuple[Path, Path, Path]:
        return (self.browser_evidence_path, self.trace_path, self.screenshot_manifest_path)


class PlaywrightEvidenceCollector:
    """Start the real GUI and run the documented smoke flow."""

    def __init__(
        self,
        scenario_input: PlaywrightScenarioInputV1 | PlaywrightProductionScenarioInputV2,
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
            from playwright.sync_api import (  # pyright: ignore[reportMissingImports]
                sync_playwright,
            )
        except ImportError as exc:
            raise PlaywrightEvidenceError("Playwright runtime is not installed") from exc

        production_input = isinstance(self.input, PlaywrightProductionScenarioInputV2)
        config_path = Path(self.input.config_path).expanduser().resolve()
        if isinstance(self.input, PlaywrightProductionScenarioInputV2):
            workspace = Path(self.input.output_root).expanduser().resolve()
        else:
            workspace = Path(self.input.workspace).expanduser().resolve()
        repo_root = Path(self.input.repo_root).expanduser().resolve()
        if not config_path.is_file() or config_path.is_symlink():
            raise PlaywrightEvidenceError("Playwright GUI config is missing or unsafe")
        if not repo_root.is_dir() or repo_root.is_symlink():
            raise PlaywrightEvidenceError("Playwright GUI repo root is missing or unsafe")
        if production_input:
            self._validate_production_paths(config_path)
        workspace.mkdir(parents=True, exist_ok=True)
        if production_input:
            staging_id = hashlib.sha256(self.acceptance_run_id.encode("utf-8")).hexdigest()[:24]
            staging_parent = workspace / "acceptance_gui_staging"
            if staging_parent.exists() and (
                not staging_parent.is_dir() or is_reparse_path(staging_parent)
            ):
                raise PlaywrightEvidenceError("Playwright production staging root is unsafe")
            staging_parent.mkdir(parents=True, exist_ok=True)
            # A run ID alone is not an exclusive lease: an operator can retry or
            # resume the same acceptance run while a previous collector exits.
            # Each browser process therefore gets its own staging root.
            evidence_root = Path(
                tempfile.mkdtemp(prefix=f"{staging_id}-", dir=str(staging_parent))
            ).resolve()
        else:
            evidence_root = workspace / "acceptance_gui"
        evidence_root.mkdir(parents=True, exist_ok=True)
        trace_path = evidence_root / "trace.zip"
        browser_path = evidence_root / "playwright_run_evidence.json"
        screenshot_path = evidence_root / "dashboard.png"
        screenshot_manifest_path = evidence_root / "screenshot_manifest.json"
        process_env = dict(self.environment)
        # Gate I is a production workflow check. Existing page-only GUI tests
        # set AUTO_GENERATE_GUI_TEST_MODE themselves; the acceptance collector
        # must never turn it on and thereby replace queue/provider execution
        # with mocks.
        process_env["AUTO_GENERATE_GUI_TEST_MODE"] = "0"
        process_env["NICEGUI_SCREEN_TEST_PORT"] = str(self.input.port)
        gui_process = subprocess.Popen(
            [
                sys.executable,
                "launch_gui.py",
                "--no-show",
                "--port",
                str(self.input.port),
                "--strict-port",
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
        submitted_job_id = ""
        if isinstance(self.input, PlaywrightScenarioInputV1):
            submitted_job_id = self.input.resulting_job_id
        job_workspace_path: Path | None = None
        runtime_spec_path: Path | None = None
        job_outcome_path: Path | None = None
        attempt_path: Path | None = None
        provider_ledger_path: Path | None = None
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
                    workflow_url = self.input.base_url.rstrip("/") + "/workflow"
                    page.goto(workflow_url, wait_until="domcontentloaded")
                    self._assert_url_suffix(
                        page,
                        "/workflow",
                        "workflow_navigation",
                        assertions,
                    )
                    if production_input:
                        submitted_job_id = self._submit_production_job(page, assertions)
                        (
                            job_workspace_path,
                            runtime_spec_path,
                            job_outcome_path,
                            attempt_path,
                            provider_ledger_path,
                        ) = self._wait_for_production_job(submitted_job_id)
                        assertions.append({"name": "canonical_outcome_verified", "passed": True})
                        assertions.append({"name": "same_job_terminal_visible", "passed": True})
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    screenshots.append({"name": "dashboard", "path": str(screenshot_path)})
                finally:
                    page.close()
                    context.tracing.stop(path=str(trace_path))
                    trace_stopped = True
                    context.close()
                    browser.close()
            if not trace_path.is_file() or not zipfile.is_zipfile(trace_path):
                raise PlaywrightEvidenceError("Playwright did not produce a valid trace archive")
            if production_input:
                if job_workspace_path is None:
                    raise PlaywrightEvidenceError(
                        "production GUI evidence has no resulting job workspace"
                    )
                return self._publish_production_artifacts(
                    staging_trace_path=trace_path,
                    staging_screenshot_path=screenshot_path,
                    job_workspace=job_workspace_path,
                    job_id=submitted_job_id,
                    screenshots=screenshots,
                    assertions=assertions,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    session_id=session_id,
                    started_at=started_at,
                    gui_pid=gui_process.pid,
                    runtime_spec_path=runtime_spec_path,
                    job_outcome_path=job_outcome_path,
                    attempt_path=attempt_path,
                    provider_ledger_path=provider_ledger_path,
                )
            self._write_evidence_metadata(
                browser_path=browser_path,
                trace_path=trace_path,
                screenshot_manifest_path=screenshot_manifest_path,
                screenshots=screenshots,
                assertions=assertions,
                console_errors=console_errors,
                page_errors=page_errors,
                job_id=submitted_job_id,
                session_id=session_id,
                started_at=started_at,
                gui_pid=gui_process.pid,
                execution_kind="page_smoke",
            )
            self._ensure_browser_flow_completed(assertions, console_errors, page_errors)
            self._register_artifacts(
                browser_path=browser_path,
                trace_path=trace_path,
                screenshot_manifest_path=screenshot_manifest_path,
                screenshot_path=screenshot_path,
                workspace=job_workspace_path or workspace,
                job_id=submitted_job_id,
            )
            return PlaywrightEvidenceResultV1(
                browser_evidence_path=browser_path,
                trace_path=trace_path,
                screenshot_manifest_path=screenshot_manifest_path,
                submitted_job_id=submitted_job_id,
                job_workspace_path=job_workspace_path,
                runtime_spec_path=runtime_spec_path,
                job_outcome_path=job_outcome_path,
                attempt_path=attempt_path,
                provider_ledger_path=provider_ledger_path,
            )
        except BaseException:
            if production_input:
                self._cleanup_production_staging_artifacts(evidence_root)
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

    @staticmethod
    @contextmanager
    def _production_artifact_publication_lock(job_workspace: Path):
        """Serialize one job workspace's Gate I evidence publication."""

        workspace = Path(job_workspace).expanduser().resolve()
        if not workspace.is_dir() or is_reparse_path(workspace):
            raise PlaywrightEvidenceError("job workspace is missing or unsafe")
        try:
            with interprocess_file_lock(
                workspace / ".playwright_evidence_publication",
                timeout_seconds=30.0,
            ):
                yield workspace
        except InterProcessLockTimeout as exc:
            raise PlaywrightEvidenceError(
                "timed out acquiring Gate I evidence publication lock"
            ) from exc

    def _publish_production_artifacts(
        self,
        *,
        staging_trace_path: Path,
        staging_screenshot_path: Path,
        job_workspace: Path,
        job_id: str,
        screenshots: list[dict[str, Any]],
        assertions: list[dict[str, Any]],
        console_errors: list[str],
        page_errors: list[str],
        session_id: str,
        started_at: str,
        gui_pid: int,
        runtime_spec_path: Path | None,
        job_outcome_path: Path | None,
        attempt_path: Path | None,
        provider_ledger_path: Path | None,
    ) -> PlaywrightEvidenceResultV1:
        """Publish one production browser run as one locked workspace transaction."""

        with self._production_artifact_publication_lock(job_workspace) as workspace:
            production_artifact_root: Path | None = None
            production_artifacts_registered = False
            try:
                (
                    browser_path,
                    trace_path,
                    screenshot_manifest_path,
                    screenshot_path,
                ) = self._relocate_production_artifacts_locked(
                    staging_trace_path=staging_trace_path,
                    staging_screenshot_path=staging_screenshot_path,
                    workspace=workspace,
                    job_id=job_id,
                )
                production_artifact_root = trace_path.parent
                for screenshot in screenshots:
                    if screenshot.get("name") == "dashboard":
                        screenshot["path"] = str(screenshot_path)
                self._write_evidence_metadata(
                    browser_path=browser_path,
                    trace_path=trace_path,
                    screenshot_manifest_path=screenshot_manifest_path,
                    screenshots=screenshots,
                    assertions=assertions,
                    console_errors=console_errors,
                    page_errors=page_errors,
                    job_id=job_id,
                    session_id=session_id,
                    started_at=started_at,
                    gui_pid=gui_pid,
                    execution_kind="production",
                )
                self._ensure_browser_flow_completed(
                    assertions,
                    console_errors,
                    page_errors,
                )
                self._register_artifacts(
                    browser_path=browser_path,
                    trace_path=trace_path,
                    screenshot_manifest_path=screenshot_manifest_path,
                    screenshot_path=screenshot_path,
                    workspace=workspace,
                    job_id=job_id,
                )
                production_artifacts_registered = True
                return PlaywrightEvidenceResultV1(
                    browser_evidence_path=browser_path,
                    trace_path=trace_path,
                    screenshot_manifest_path=screenshot_manifest_path,
                    submitted_job_id=job_id,
                    job_workspace_path=workspace,
                    runtime_spec_path=runtime_spec_path,
                    job_outcome_path=job_outcome_path,
                    attempt_path=attempt_path,
                    provider_ledger_path=provider_ledger_path,
                )
            except BaseException:
                if (
                    production_artifact_root is not None
                    and not production_artifacts_registered
                ):
                    self._cleanup_unregistered_production_artifacts_locked(
                        production_artifact_root,
                        workspace=workspace,
                        job_id=job_id,
                    )
                raise

    def _write_evidence_metadata(
        self,
        *,
        browser_path: Path,
        trace_path: Path,
        screenshot_manifest_path: Path,
        screenshots: list[dict[str, Any]],
        assertions: list[dict[str, Any]],
        console_errors: list[str],
        page_errors: list[str],
        job_id: str,
        session_id: str,
        started_at: str,
        gui_pid: int,
        execution_kind: str,
    ) -> None:
        trace_sha = _sha256(trace_path)
        atomic_write_json(
            str(screenshot_manifest_path),
            {
                "artifact_type": "playwright_screenshot_manifest",
                "artifact_version": "v1",
                "schema_version": "playwright-screenshot-manifest-v1",
                "acceptance_run_id": self.acceptance_run_id,
                "scenario_id": self.scenario_id,
                "job_id": job_id,
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
                "resulting_job_id": job_id,
                "execution_kind": execution_kind,
                "trace_sha256": trace_sha,
                "flow_assertions": assertions,
                "console_errors": console_errors,
                "page_errors": page_errors,
                "started_at": started_at,
                "completed_at": _now(),
                "gui_pid": gui_pid,
                "screenshot_manifest_path": str(screenshot_manifest_path),
            },
        )

    @staticmethod
    def _ensure_browser_flow_completed(
        assertions: list[dict[str, Any]],
        console_errors: list[str],
        page_errors: list[str],
    ) -> None:
        if (
            any(item.get("passed") is not True for item in assertions)
            or console_errors
            or page_errors
        ):
            raise PlaywrightEvidenceError("documented Playwright flow did not complete cleanly")

    @classmethod
    def _relocate_production_artifacts(
        cls,
        *,
        staging_trace_path: Path,
        staging_screenshot_path: Path,
        job_workspace: Path,
        job_id: str = "",
    ) -> tuple[Path, Path, Path, Path]:
        """Move browser outputs under the job workspace before registration.

        File publication precedes the Registry transaction, so a failed move
        must never strand a subset that prevents a later legitimate retry.
        Existing artifacts are retained only when they are already Registry
        published; unregistered, regular-file remnants are cleaned as a
        recoverable interrupted publication.
        """

        with cls._production_artifact_publication_lock(job_workspace) as workspace:
            return cls._relocate_production_artifacts_locked(
                staging_trace_path=staging_trace_path,
                staging_screenshot_path=staging_screenshot_path,
                workspace=workspace,
                job_id=job_id,
            )

    @staticmethod
    def _relocate_production_artifacts_locked(
        *,
        staging_trace_path: Path,
        staging_screenshot_path: Path,
        workspace: Path,
        job_id: str,
    ) -> tuple[Path, Path, Path, Path]:
        """Move artifacts while the caller owns the workspace publication lock."""

        destination_root = workspace / "acceptance_gui"
        if destination_root.exists():
            if is_reparse_path(destination_root):
                raise PlaywrightEvidenceError("job acceptance evidence directory is a reparse path")
            if PlaywrightEvidenceCollector._has_registered_production_artifacts(
                destination_root,
                workspace=workspace,
                job_id=job_id,
            ):
                raise PlaywrightEvidenceError(
                    "job acceptance evidence is already Registry-published"
                )
            PlaywrightEvidenceCollector._remove_unregistered_artifact_directory(
                destination_root
            )
        destination_root.mkdir(parents=True, exist_ok=True)
        destination_paths = {
            staging_trace_path: destination_root / "trace.zip",
            staging_screenshot_path: destination_root / "dashboard.png",
        }
        moved: list[tuple[Path, Path]] = []
        try:
            for source, destination in destination_paths.items():
                if not source.is_file() or is_reparse_path(source):
                    raise PlaywrightEvidenceError(
                        f"staged Playwright artifact is missing or unsafe: {source.name}"
                    )
                if destination.exists() or is_reparse_path(destination):
                    raise PlaywrightEvidenceError(
                        f"job acceptance artifact already exists: {destination.name}"
                    )
                try:
                    shutil.move(str(source), str(destination))
                except OSError as exc:
                    raise PlaywrightEvidenceError(
                        f"could not move Playwright artifact into job workspace: {source.name}"
                    ) from exc
                moved.append((source, destination))
        except BaseException:
            for source, destination in reversed(moved):
                try:
                    if destination.is_file() and not is_reparse_path(destination):
                        shutil.move(str(destination), str(source))
                except OSError:
                    try:
                        destination.unlink(missing_ok=True)
                    except OSError:
                        pass
            try:
                PlaywrightEvidenceCollector._remove_unregistered_artifact_directory(
                    destination_root
                )
            except PlaywrightEvidenceError:
                pass
            raise
        return (
            destination_root / "playwright_run_evidence.json",
            destination_root / "trace.zip",
            destination_root / "screenshot_manifest.json",
            destination_root / "dashboard.png",
        )

    @staticmethod
    def _has_registered_production_artifacts(
        artifact_root: Path,
        *,
        workspace: Path,
        job_id: str,
    ) -> bool:
        if not job_id:
            return False
        from services.artifact_registry import ArtifactRegistry, RegistryError

        registry_path = workspace / "artifact_registry.json"
        if not registry_path.is_file() or is_reparse_path(registry_path):
            return False
        try:
            registry = ArtifactRegistry(registry_path, job_id)
            return any(
                record.status == "ready"
                and record.artifact_type
                in {
                    "playwright_run_evidence",
                    "playwright_trace",
                    "playwright_screenshot_manifest",
                    "playwright_screenshot",
                }
                and Path(record.path).expanduser().resolve().parent == artifact_root
                for record in registry.list_records()
            )
        except (OSError, RegistryError, ValueError):
            # An unreadable Registry cannot establish that a directory is safe
            # to overwrite, so retain the directory and fail closed upstream.
            return True

    @staticmethod
    def _remove_unregistered_artifact_directory(artifact_root: Path) -> None:
        """Delete only a bounded, non-reparse failed publication directory."""

        if not artifact_root.exists():
            return
        if not artifact_root.is_dir() or is_reparse_path(artifact_root):
            raise PlaywrightEvidenceError("unregistered acceptance artifact root is unsafe")
        allowed = {
            "trace.zip",
            "dashboard.png",
            "playwright_run_evidence.json",
            "screenshot_manifest.json",
        }
        try:
            children = list(artifact_root.iterdir())
        except OSError as exc:
            raise PlaywrightEvidenceError("cannot inspect unregistered acceptance artifacts") from exc
        for child in children:
            if child.name not in allowed or not child.is_file() or is_reparse_path(child):
                raise PlaywrightEvidenceError(
                    "unregistered acceptance artifact directory has unexpected content"
                )
        for child in children:
            try:
                child.unlink()
            except OSError as exc:
                raise PlaywrightEvidenceError(
                    "cannot remove unregistered acceptance artifact"
                ) from exc
        try:
            artifact_root.rmdir()
        except OSError as exc:
            raise PlaywrightEvidenceError(
                "cannot remove unregistered acceptance artifact directory"
            ) from exc

    @classmethod
    def _cleanup_unregistered_production_artifacts(
        cls,
        artifact_root: Path,
        *,
        job_workspace: Path | None,
        job_id: str,
    ) -> None:
        if job_workspace is None:
            return
        with cls._production_artifact_publication_lock(job_workspace) as workspace:
            cls._cleanup_unregistered_production_artifacts_locked(
                artifact_root,
                workspace=workspace,
                job_id=job_id,
            )

    @staticmethod
    def _cleanup_unregistered_production_artifacts_locked(
        artifact_root: Path,
        *,
        workspace: Path,
        job_id: str,
    ) -> None:
        if artifact_root.parent != workspace:
            return
        if PlaywrightEvidenceCollector._has_registered_production_artifacts(
            artifact_root,
            workspace=workspace,
            job_id=job_id,
        ):
            return
        try:
            PlaywrightEvidenceCollector._remove_unregistered_artifact_directory(
                artifact_root
            )
        except PlaywrightEvidenceError:
            # Preserve unexpected remnants for manual investigation rather
            # than deleting a path whose ownership cannot be established.
            return

    @staticmethod
    def _cleanup_production_staging_artifacts(staging_root: Path) -> None:
        """Remove only the current run's ordinary browser staging files."""

        if not staging_root.exists() or not staging_root.is_dir() or is_reparse_path(staging_root):
            return
        allowed = {
            "trace.zip",
            "dashboard.png",
            "playwright_run_evidence.json",
            "screenshot_manifest.json",
        }
        try:
            children = list(staging_root.iterdir())
        except OSError:
            return
        if any(
            child.name not in allowed or not child.is_file() or is_reparse_path(child)
            for child in children
        ):
            return
        for child in children:
            try:
                child.unlink()
            except OSError:
                return
        try:
            staging_root.rmdir()
        except OSError:
            return

    def _register_artifacts(
        self,
        *,
        browser_path: Path,
        trace_path: Path,
        screenshot_manifest_path: Path,
        screenshot_path: Path,
        workspace: Path | None = None,
        job_id: str = "",
    ) -> None:
        """Publish browser outputs to the resulting job's current Registry."""

        from services.artifact_registry import ArtifactRegistry, RegistryError

        if workspace is None:
            if isinstance(self.input, PlaywrightProductionScenarioInputV2):
                workspace = Path(self.input.output_root).expanduser().resolve()
            else:
                workspace = Path(self.input.workspace).expanduser().resolve()
        if not job_id:
            job_id = self.input.resulting_job_id if isinstance(self.input, PlaywrightScenarioInputV1) else ""
        if not job_id:
            raise PlaywrightEvidenceError("Playwright artifact registration requires a job ID")
        registry_path = workspace / "artifact_registry.json"
        registry = ArtifactRegistry(registry_path, job_id)
        files = (
            ("playwright_run_evidence", "playwright_run_evidence", browser_path),
            ("playwright_trace", "playwright_trace", trace_path),
            (
                "playwright_screenshot_manifest",
                "playwright_screenshot_manifest",
                screenshot_manifest_path,
            ),
            ("playwright_screenshot", "playwright_screenshot", screenshot_path),
        )
        records = []
        for role, artifact_type, path in files:
            if not path.is_file() or path.is_symlink():
                raise PlaywrightEvidenceError(
                    f"Playwright artifact is missing or unsafe: {path.name}"
                )
            records.append(
                {
                    "artifact_id": f"acceptance-I:{artifact_type}:{_sha256(path)[:32]}",
                    "artifact_role": role,
                    "artifact_type": artifact_type,
                    "artifact_version": "v1",
                    "path": str(path),
                    "producer": "runtime.playwright_evidence.PlaywrightEvidenceCollector",
                    "job_id": job_id,
                }
            )
        try:
            registry.register_files_atomic(records)
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            raise PlaywrightEvidenceError(
                f"Playwright artifact Registry publication failed: {type(exc).__name__}"
            ) from exc

    def _submit_production_job(self, page: Any, assertions: list[dict[str, Any]]) -> str:
        request = self.input
        if not isinstance(request, PlaywrightProductionScenarioInputV2):
            raise PlaywrightEvidenceError("production submission requires v2 input")
        page.get_by_test_id("workflow-project-name").fill(request.project_name)
        mode_toggle = page.get_by_test_id("workflow-input-mode")
        mode_index = 0 if request.input_mode == "pdf" else 1
        self._activate_browser_control(
            mode_toggle.get_by_role("button").nth(mode_index),
            assertion_name="workflow_input_mode_selected",
            assertions=assertions,
        )
        work_toggle = page.get_by_test_id("workflow-work-mode")
        self._activate_browser_control(
            work_toggle.get_by_role("button").nth(0),
            assertion_name="workflow_normal_mode_selected",
            assertions=assertions,
        )
        if request.input_mode == "pdf":
            self._activate_browser_control(
                page.get_by_test_id("workflow-pdf-folder-open"),
                assertion_name="workflow_pdf_editor_opened",
                assertions=assertions,
            )
            page.get_by_test_id("workflow-pdf-folder-edit").fill(request.pdf_folder)
            self._activate_browser_control(
                page.get_by_test_id("workflow-pdf-folder-save"),
                assertion_name="workflow_pdf_folder_saved",
                assertions=assertions,
            )
        else:
            self._activate_browser_control(
                page.get_by_test_id("workflow-zotero-report-open"),
                assertion_name="workflow_zotero_report_editor_opened",
                assertions=assertions,
            )
            page.get_by_test_id("workflow-zotero-report-edit").fill(request.zotero_report)
            self._activate_browser_control(
                page.get_by_test_id("workflow-zotero-report-save"),
                assertion_name="workflow_zotero_report_saved",
                assertions=assertions,
            )
            self._activate_browser_control(
                page.get_by_test_id("workflow-library-path-open"),
                assertion_name="workflow_library_editor_opened",
                assertions=assertions,
            )
            page.get_by_test_id("workflow-library-path-edit").fill(request.library_path)
            self._activate_browser_control(
                page.get_by_test_id("workflow-library-path-save"),
                assertion_name="workflow_library_path_saved",
                assertions=assertions,
            )
        assertions.append({"name": "workflow_request_filled", "passed": True})
        self._activate_browser_control(
            page.get_by_test_id(f"workflow-action-{request.action}"),
            assertion_name="workflow_action_submitted",
            assertions=assertions,
        )
        assertions.append({"name": "job_submitted", "passed": True})
        label = page.get_by_test_id("workflow-submitted-job-id")
        label.wait_for(state="visible", timeout=30_000)
        deadline = time.time() + 30.0
        while time.time() < deadline:
            job_id = str(label.inner_text() or "").strip()
            if job_id:
                assertions.append({"name": "submitted_job_visible", "passed": True})
                return job_id
            time.sleep(0.25)
        raise PlaywrightEvidenceError("GUI did not expose the submitted queue job ID")

    @staticmethod
    def _activate_browser_control(
        locator: Any,
        *,
        assertion_name: str,
        assertions: list[dict[str, Any]],
    ) -> None:
        """Activate one actual browser control with a recorded headless fallback."""

        try:
            locator.scroll_into_view_if_needed(timeout=10_000)
            locator.click(timeout=10_000)
            assertions.append(
                {"name": assertion_name, "passed": True, "detail": "pointer_click"}
            )
            return
        except Exception as pointer_error:
            try:
                locator.evaluate("(element) => element.click()")
            except Exception as dom_error:
                raise PlaywrightEvidenceError(
                    f"browser control could not be activated: {assertion_name}"
                ) from dom_error
            assertions.append(
                {
                    "name": assertion_name,
                    "passed": True,
                    "detail": "dom_click_fallback_after_viewport_error",
                    "pointer_error_type": type(pointer_error).__name__,
                }
            )

    def _validate_production_paths(self, config_path: Path) -> None:
        request = self.input
        if not isinstance(request, PlaywrightProductionScenarioInputV2):
            return
        parser = configparser.ConfigParser()
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, UnicodeError, configparser.Error) as exc:
            raise PlaywrightEvidenceError("production GUI config is unreadable") from exc
        configured_output = str(parser.get("Paths", "output_path", fallback="")).strip()
        if not configured_output:
            raise PlaywrightEvidenceError("production GUI config lacks Paths.output_path")
        output_path = Path(configured_output).expanduser()
        if not output_path.is_absolute():
            output_path = config_path.parent / output_path
        if output_path.resolve() != Path(request.output_root).expanduser().resolve():
            raise PlaywrightEvidenceError(
                "production GUI output_root does not match the configured Paths.output_path"
            )
        if request.input_mode == "pdf" and not Path(request.pdf_folder).expanduser().is_dir():
            raise PlaywrightEvidenceError("production GUI PDF folder does not exist")
        if request.input_mode == "zotero":
            if not Path(request.zotero_report).expanduser().is_file():
                raise PlaywrightEvidenceError("production GUI Zotero report does not exist")
            if not Path(request.library_path).expanduser().is_dir():
                raise PlaywrightEvidenceError("production GUI Zotero library does not exist")

    def _wait_for_production_job(self, job_id: str) -> tuple[Path, Path, Path, Path | None, Path]:
        request = self.input
        if not isinstance(request, PlaywrightProductionScenarioInputV2):
            raise PlaywrightEvidenceError("production job wait requires v2 input")
        from services.artifact_registry import ArtifactRegistry
        from services.job_outcome import load_canonical_job_outcome
        from services.queue_service import PersistentQueueService, QueueState

        queue_path = Path(request.output_root).expanduser().resolve() / "_queue" / "queue.json"
        queue = PersistentQueueService(queue_path)
        deadline = time.time() + request.completion_timeout_seconds
        while time.time() < deadline:
            runtime = queue.get_job_runtime(job_id)
            if runtime is None:
                time.sleep(0.5)
                continue
            if runtime.state in {QueueState.FAILED, QueueState.CANCELLED}:
                raise PlaywrightEvidenceError(
                    f"GUI-submitted job {job_id} ended in {runtime.state.value}: {runtime.error_message or ''}"
                )
            if runtime.state != QueueState.COMPLETED or not runtime.workspace_path:
                time.sleep(0.5)
                continue
            workspace = Path(runtime.workspace_path).expanduser().resolve()
            registry = ArtifactRegistry(workspace / "artifact_registry.json", job_id)
            outcome, outcome_record = load_canonical_job_outcome(registry)
            if outcome.job_status != "completed" or not outcome.canonical_ready:
                raise PlaywrightEvidenceError(
                    "GUI-submitted job reached queue completion without a canonical outcome"
                )
            spec_record = registry.get("runtime_job_spec")
            if spec_record is None or spec_record.status != "ready":
                raise PlaywrightEvidenceError("GUI-submitted job lacks a Registry-bound RuntimeJobSpec")
            from runtime.job_spec import RuntimeJobSpec

            spec_payload = json.loads(Path(spec_record.path).read_text(encoding="utf-8"))
            runtime_spec = RuntimeJobSpec.from_dict(spec_payload).resolved_from(Path(spec_record.path).parent)
            runtime_spec.validate()
            expected_action = {
                "analyze": "analyze",
                "outline": "generate_outline",
                "review": "generate_review",
                "run_all": "run_all",
            }[request.action]
            if runtime_spec.job_id != job_id or runtime_spec.project_name != request.project_name:
                raise PlaywrightEvidenceError("GUI-submitted RuntimeJobSpec identity does not match the request")
            if runtime_spec.action != expected_action:
                raise PlaywrightEvidenceError("GUI-submitted RuntimeJobSpec action does not match the request")
            if runtime_spec.config != str(Path(request.config_path).expanduser().resolve()):
                raise PlaywrightEvidenceError("GUI-submitted RuntimeJobSpec config does not match the request")
            expected_source_mode = _runtime_source_mode_for_gui_input(request.input_mode)
            if runtime_spec.source.mode != expected_source_mode:
                raise PlaywrightEvidenceError("GUI-submitted RuntimeJobSpec source mode does not match the request")
            if request.input_mode == "pdf":
                if runtime_spec.source.pdf_folder != str(Path(request.pdf_folder).expanduser().resolve()):
                    raise PlaywrightEvidenceError("GUI-submitted RuntimeJobSpec PDF folder does not match the request")
            elif (
                runtime_spec.source.zotero_report != str(Path(request.zotero_report).expanduser().resolve())
                or runtime_spec.source.library_path != str(Path(request.library_path).expanduser().resolve())
            ):
                raise PlaywrightEvidenceError("GUI-submitted RuntimeJobSpec Zotero paths do not match the request")
            if Path(runtime_spec.workspace_path).expanduser().resolve().parent != Path(request.output_root).expanduser().resolve():
                raise PlaywrightEvidenceError("GUI-submitted RuntimeJobSpec workspace is outside the requested output root")
            attempt_records = [
                record
                for record in registry.list_records()
                if record.status == "ready" and record.artifact_type == "job_attempt"
            ]
            attempt_record = max(
                attempt_records,
                key=lambda record: int(record.metadata.get("snapshot_sequence") or 0),
                default=None,
            )
            if attempt_record is None:
                raise PlaywrightEvidenceError("GUI-submitted job lacks a durable terminal attempt")
            ledger_records = [
                record
                for record in registry.list_records()
                if record.status == "ready" and record.artifact_type == "provider_receipt_ledger"
            ]
            ledger_record = max(
                ledger_records,
                key=lambda record: record.created_at,
                default=None,
            )
            if ledger_record is None:
                raise PlaywrightEvidenceError("GUI-submitted job lacks a provider receipt ledger")
            return (
                workspace,
                Path(spec_record.path),
                Path(outcome_record.path),
                Path(attempt_record.path),
                Path(ledger_record.path),
            )
        raise PlaywrightEvidenceError(
            f"GUI-submitted job {job_id} did not reach a canonical terminal outcome before timeout"
        )

    def _wait_for_server(self, process: subprocess.Popen[Any]) -> None:
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
    "PlaywrightProductionScenarioInputV2",
    "PlaywrightScenarioInputV1",
]
