from __future__ import annotations

import json
import multiprocessing
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from launch_gui import _pick_available_port
from runtime.playwright_evidence import (
    PlaywrightEvidenceCollector,
    PlaywrightEvidenceError,
    PlaywrightProductionScenarioInputV2,
    PlaywrightScenarioInputV1,
    _runtime_source_mode_for_gui_input,
)
from services.artifact_registry import ArtifactRegistry


def _write_concurrent_trace(path: Path, payload: bytes) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("trace.trace", payload)


def _concurrent_production_publication_worker(
    workspace_path: str,
    staging_path: str,
    label: str,
    first_ready: object,
    second_lock_attempted: object,
    release_first: object,
    results: object,
) -> None:
    """Run one real publication transaction in a spawned Python process."""

    import runtime.playwright_evidence as evidence_module

    workspace = Path(workspace_path)
    staging = Path(staging_path)
    collector = PlaywrightEvidenceCollector(
        PlaywrightProductionScenarioInputV2(
            base_url="http://127.0.0.1:8123",
            config_path=str(workspace / "config.ini"),
            repo_root=str(workspace),
            output_root=str(workspace.parent),
            input_mode="pdf",
            pdf_folder=str(workspace / "pdfs"),
            zotero_report="",
            library_path="",
            project_name="concurrency",
            work_mode="normal",
            action="analyze",
            port=8123,
        ),
        acceptance_run_id="acceptance-concurrent",
        scenario_id="I",
        final_executable_sha="a" * 40,
    )

    if label == "first":
        def fail_after_metadata(**_kwargs: object) -> None:
            first_ready.set()  # type: ignore[attr-defined]
            if not release_first.wait(15):  # type: ignore[attr-defined]
                raise RuntimeError("test did not release first publication")
            raise PlaywrightEvidenceError("controlled first registration failure")

        collector._register_artifacts = fail_after_metadata  # type: ignore[method-assign]
    else:
        real_lock = evidence_module.interprocess_file_lock

        @contextmanager
        def announce_lock_attempt(*args: object, **kwargs: object):
            second_lock_attempted.set()  # type: ignore[attr-defined]
            with real_lock(*args, **kwargs):
                yield

        evidence_module.interprocess_file_lock = announce_lock_attempt

    try:
        result = collector._publish_production_artifacts(
            staging_trace_path=staging / "trace.zip",
            staging_screenshot_path=staging / "dashboard.png",
            job_workspace=workspace,
            job_id="job-concurrent",
            screenshots=[{"name": "dashboard", "path": str(staging / "dashboard.png")}],
            assertions=[{"name": "browser_flow", "passed": True}],
            console_errors=[],
            page_errors=[],
            session_id=f"session-{label}",
            started_at="2026-09-15T00:00:00Z",
            gui_pid=1,
            runtime_spec_path=None,
            job_outcome_path=None,
            attempt_path=None,
            provider_ledger_path=None,
        )
    except PlaywrightEvidenceError as exc:
        results.put((label, "error", str(exc)))  # type: ignore[attr-defined]
    else:
        results.put((label, "success", str(result.trace_path)))  # type: ignore[attr-defined]


def _input_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "artifact_type": "acceptance_gui_input",
        "artifact_version": "v1",
        "schema_version": "acceptance-gui-input-v1",
        "base_url": "http://127.0.0.1:8080",
        "config_path": "gui.ini",
        "workspace": "workspace-gui",
        "resulting_job_id": "job-gui",
        "repo_root": ".",
        "port": 8080,
    }
    payload.update(overrides)
    return payload


def test_playwright_input_requires_localhost_origin() -> None:
    with pytest.raises(PlaywrightEvidenceError, match="localhost"):
        PlaywrightScenarioInputV1.from_mapping(
            _input_payload(base_url="https://remote.example.test")
        )


def test_playwright_input_requires_a_resulting_runtime_job() -> None:
    with pytest.raises(PlaywrightEvidenceError, match="resulting_job_id"):
        PlaywrightScenarioInputV1.from_mapping(
            _input_payload(resulting_job_id="")
        )


def test_playwright_input_requires_launcher_url_port_match() -> None:
    with pytest.raises(PlaywrightEvidenceError, match="port"):
        PlaywrightScenarioInputV1.from_mapping(
            _input_payload(base_url="http://127.0.0.1:8081", port=8080)
        )


def test_launcher_strict_port_rejects_browser_unsafe_port() -> None:
    with pytest.raises(RuntimeError, match="unavailable or browser-unsafe"):
        _pick_available_port(6566, strict=True)


def test_launcher_strict_port_rejects_an_occupied_port() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        with pytest.raises(RuntimeError, match="unavailable or browser-unsafe"):
            _pick_available_port(port, strict=True)


def test_production_playwright_input_describes_request_without_preselected_job(tmp_path: Path) -> None:
    parsed = PlaywrightProductionScenarioInputV2.from_mapping(
        {
            "artifact_type": "acceptance_gui_input",
            "artifact_version": "v2",
            "schema_version": "acceptance-gui-input-v2",
            "execution_kind": "production",
            "base_url": "http://127.0.0.1:8080",
            "config_path": str(tmp_path / "config.ini"),
            "repo_root": str(tmp_path),
            "output_root": str(tmp_path / "output"),
            "input_mode": "pdf",
            "pdf_folder": str(tmp_path / "pdfs"),
            "project_name": "gui-production",
            "work_mode": "normal",
            "action": "analyze",
            "port": 8080,
        }
    )

    assert parsed.project_name == "gui-production"
    assert parsed.action == "analyze"
    assert not hasattr(parsed, "resulting_job_id")


def test_production_playwright_input_rejects_page_smoke_execution_kind(tmp_path: Path) -> None:
    payload = {
        "artifact_type": "acceptance_gui_input",
        "artifact_version": "v2",
        "schema_version": "acceptance-gui-input-v2",
        "execution_kind": "page_smoke",
        "base_url": "http://127.0.0.1:8080",
        "config_path": str(tmp_path / "config.ini"),
        "repo_root": str(tmp_path),
        "output_root": str(tmp_path / "output"),
        "input_mode": "pdf",
        "pdf_folder": str(tmp_path / "pdfs"),
        "project_name": "gui-production",
        "action": "analyze",
        "port": 8080,
    }
    with pytest.raises(PlaywrightEvidenceError, match="execution_kind"):
        PlaywrightProductionScenarioInputV2.from_mapping(payload)


def test_production_gui_input_mode_maps_to_runtime_source_mode() -> None:
    assert _runtime_source_mode_for_gui_input("pdf") == "direct"
    assert _runtime_source_mode_for_gui_input("zotero") == "zotero"
    with pytest.raises(PlaywrightEvidenceError, match="unsupported GUI production input mode"):
        _runtime_source_mode_for_gui_input("unknown")


def test_production_artifacts_are_relocated_into_job_workspace(tmp_path: Path) -> None:
    staging = tmp_path / "output" / "acceptance_gui_staging" / "run"
    staging.mkdir(parents=True)
    trace = staging / "trace.zip"
    screenshot = staging / "dashboard.png"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("trace.trace", "{}")
    screenshot.write_bytes(b"png")
    job_workspace = tmp_path / "output" / "project__job"
    job_workspace.mkdir(parents=True)
    collector = PlaywrightEvidenceCollector(
        PlaywrightProductionScenarioInputV2(
            base_url="http://127.0.0.1:8080",
            config_path=str(tmp_path / "config.ini"),
            repo_root=str(tmp_path),
            output_root=str(tmp_path / "output"),
            input_mode="pdf",
            pdf_folder=str(tmp_path / "pdfs"),
            zotero_report="",
            library_path="",
            project_name="project",
            work_mode="normal",
            action="analyze",
            port=8080,
        ),
        acceptance_run_id="acceptance-i",
        scenario_id="I",
        final_executable_sha="a" * 40,
    )

    browser_path, trace_path, manifest_path, screenshot_path = collector._relocate_production_artifacts(
        staging_trace_path=trace,
        staging_screenshot_path=screenshot,
        job_workspace=job_workspace,
    )

    assert trace_path.parent == job_workspace / "acceptance_gui"
    assert screenshot_path.parent == job_workspace / "acceptance_gui"
    assert browser_path.parent == manifest_path.parent == trace_path.parent
    assert trace_path.is_file()
    assert screenshot_path.is_file()
    assert not trace.exists()
    assert not screenshot.exists()


def test_production_artifact_relocation_rolls_back_a_partial_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    trace = staging / "trace.zip"
    screenshot = staging / "dashboard.png"
    trace.write_bytes(b"trace")
    screenshot.write_bytes(b"png")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_move = __import__("runtime.playwright_evidence", fromlist=["shutil"]).shutil.move
    calls = 0

    def fail_second_move(source: str, destination: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("controlled move failure")
        return real_move(source, destination)

    monkeypatch.setattr("runtime.playwright_evidence.shutil.move", fail_second_move)

    with pytest.raises(PlaywrightEvidenceError, match="could not move"):
        PlaywrightEvidenceCollector._relocate_production_artifacts(
            staging_trace_path=trace,
            staging_screenshot_path=screenshot,
            job_workspace=workspace,
        )

    assert trace.is_file()
    assert screenshot.is_file()
    assert not (workspace / "acceptance_gui").exists()


def test_production_artifact_relocation_recovers_only_unregistered_remnants(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    destination = workspace / "acceptance_gui"
    destination.mkdir(parents=True)
    (destination / "trace.zip").write_bytes(b"interrupted")
    staging = tmp_path / "staging"
    staging.mkdir()
    trace = staging / "trace.zip"
    screenshot = staging / "dashboard.png"
    trace.write_bytes(b"fresh-trace")
    screenshot.write_bytes(b"fresh-png")

    _, trace_path, _, screenshot_path = PlaywrightEvidenceCollector._relocate_production_artifacts(
        staging_trace_path=trace,
        staging_screenshot_path=screenshot,
        job_workspace=workspace,
        job_id="job-retry",
    )

    assert trace_path.read_bytes() == b"fresh-trace"
    assert screenshot_path.read_bytes() == b"fresh-png"


def test_production_artifact_relocation_refuses_registry_published_destination(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    destination = workspace / "acceptance_gui"
    destination.mkdir(parents=True)
    published_trace = destination / "trace.zip"
    with zipfile.ZipFile(published_trace, "w") as archive:
        archive.writestr("trace.trace", "published")
    registry = ArtifactRegistry(workspace / "artifact_registry.json", "job-published")
    registry.register_file(
        artifact_id="acceptance-I:playwright_trace:published",
        artifact_role="playwright_trace",
        artifact_type="playwright_trace",
        artifact_version="v1",
        path=published_trace,
        producer="tests",
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    trace = staging / "trace.zip"
    screenshot = staging / "dashboard.png"
    trace.write_bytes(b"fresh")
    screenshot.write_bytes(b"fresh")

    with pytest.raises(PlaywrightEvidenceError, match="Registry-published"):
        PlaywrightEvidenceCollector._relocate_production_artifacts(
            staging_trace_path=trace,
            staging_screenshot_path=screenshot,
            job_workspace=workspace,
            job_id="job-published",
        )

    with zipfile.ZipFile(published_trace) as archive:
        assert archive.read("trace.trace") == b"published"
    assert trace.read_bytes() == b"fresh"
    assert screenshot.read_bytes() == b"fresh"


def test_concurrent_production_publication_keeps_second_collectors_evidence(
    tmp_path: Path,
) -> None:
    """A failed collector must clean its own transaction before a waiter publishes."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_staging = tmp_path / "first-staging"
    second_staging = tmp_path / "second-staging"
    first_staging.mkdir()
    second_staging.mkdir()
    _write_concurrent_trace(first_staging / "trace.zip", b"first")
    _write_concurrent_trace(second_staging / "trace.zip", b"second")
    (first_staging / "dashboard.png").write_bytes(b"first-dashboard")
    (second_staging / "dashboard.png").write_bytes(b"second-dashboard")

    context = multiprocessing.get_context("spawn")
    first_ready = context.Event()
    second_lock_attempted = context.Event()
    release_first = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_concurrent_production_publication_worker,
        args=(
            str(workspace),
            str(first_staging),
            "first",
            first_ready,
            second_lock_attempted,
            release_first,
            results,
        ),
    )
    second = context.Process(
        target=_concurrent_production_publication_worker,
        args=(
            str(workspace),
            str(second_staging),
            "second",
            first_ready,
            second_lock_attempted,
            release_first,
            results,
        ),
    )
    first.start()
    try:
        assert first_ready.wait(15), "first collector did not reach locked registration"
        second.start()
        assert second_lock_attempted.wait(15), "second collector did not attempt the workspace lock"
        release_first.set()
        first.join(20)
        second.join(20)
        assert not first.is_alive(), "first collector did not stop"
        assert not second.is_alive(), "second collector did not stop"
    finally:
        release_first.set()
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(10)

    # Pull both reports without depending on OS scheduling or queue order.
    reports = [results.get(timeout=10) for _ in range(2)]
    outcomes = {label: (state, detail) for label, state, detail in reports}
    assert outcomes["first"] == ("error", "controlled first registration failure")
    assert outcomes["second"][0] == "success"

    destination = workspace / "acceptance_gui"
    with zipfile.ZipFile(destination / "trace.zip") as archive:
        assert archive.read("trace.trace") == b"second"
    assert (destination / "dashboard.png").read_bytes() == b"second-dashboard"
    browser_evidence = json.loads((destination / "playwright_run_evidence.json").read_text(encoding="utf-8"))
    screenshot_manifest = json.loads((destination / "screenshot_manifest.json").read_text(encoding="utf-8"))
    assert browser_evidence["execution_kind"] == "production"
    assert screenshot_manifest["screenshots"] == [
        {"name": "dashboard", "path": str(destination / "dashboard.png")}
    ]
    registry = ArtifactRegistry(workspace / "artifact_registry.json", "job-concurrent")
    assert {
        record.artifact_type
        for record in registry.list_records()
    } == {
        "playwright_run_evidence",
        "playwright_trace",
        "playwright_screenshot_manifest",
        "playwright_screenshot",
    }


def test_production_staging_cleanup_only_removes_expected_regular_files(tmp_path: Path) -> None:
    staging = tmp_path / "acceptance_gui_staging" / "run"
    staging.mkdir(parents=True)
    (staging / "trace.zip").write_bytes(b"trace")
    (staging / "dashboard.png").write_bytes(b"png")

    PlaywrightEvidenceCollector._cleanup_production_staging_artifacts(staging)

    assert not staging.exists()
    unsafe = tmp_path / "acceptance_gui_staging" / "unsafe"
    unsafe.mkdir(parents=True)
    (unsafe / "unexpected.txt").write_text("retain", encoding="utf-8")
    PlaywrightEvidenceCollector._cleanup_production_staging_artifacts(unsafe)
    assert (unsafe / "unexpected.txt").is_file()


def test_browser_control_fallback_is_recorded_in_evidence() -> None:
    class ViewportLimitedControl:
        def scroll_into_view_if_needed(self, **_kwargs) -> None:
            raise RuntimeError("outside viewport")

        def click(self, **_kwargs) -> None:
            raise AssertionError("pointer click must not be attempted after scroll failure")

        def evaluate(self, expression: str) -> None:
            assert expression == "(element) => element.click()"

    assertions: list[dict[str, object]] = []
    PlaywrightEvidenceCollector._activate_browser_control(
        ViewportLimitedControl(),
        assertion_name="fallback_control",
        assertions=assertions,
    )

    assert assertions == [
        {
            "name": "fallback_control",
            "passed": True,
            "detail": "dom_click_fallback_after_viewport_error",
            "pointer_error_type": "RuntimeError",
        }
    ]


def test_playwright_collector_registers_all_durable_outputs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = {
        "browser_path": workspace / "playwright_run_evidence.json",
        "trace_path": workspace / "trace.zip",
        "screenshot_manifest_path": workspace / "screenshot_manifest.json",
        "screenshot_path": workspace / "dashboard.png",
    }
    paths["browser_path"].write_text(
        json.dumps(
            {
                "artifact_type": "playwright_run_evidence",
                "artifact_version": "v1",
                "schema_version": "playwright-run-evidence-v1",
                "run_id": "acceptance-i",
                "session_id": "session-i",
                "url": "http://127.0.0.1:8123",
                "resulting_job_id": "gui-job",
                "trace_sha256": "a" * 64,
                "flow_assertions": [{"name": "dashboard", "passed": True}],
                "console_errors": [],
                "page_errors": [],
            }
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(paths["trace_path"], "w") as archive:
        archive.writestr("trace.trace", "{}")
    paths["screenshot_manifest_path"].write_text(
        json.dumps(
            {
                "artifact_type": "playwright_screenshot_manifest",
                "artifact_version": "v1",
                "schema_version": "playwright-screenshot-manifest-v1",
                "screenshots": [{"name": "dashboard", "path": "dashboard.png"}],
            }
        ),
        encoding="utf-8",
    )
    paths["screenshot_path"].write_bytes(b"png")
    scenario_input = PlaywrightScenarioInputV1(
        base_url="http://127.0.0.1:8123",
        config_path=str(tmp_path / "config.ini"),
        workspace=str(workspace),
        resulting_job_id="gui-job",
        repo_root=str(tmp_path),
        port=8123,
    )
    collector = PlaywrightEvidenceCollector(
        scenario_input,
        acceptance_run_id="acceptance-i",
        scenario_id="I",
        final_executable_sha="a" * 40,
    )

    collector._register_artifacts(**paths)

    registry = ArtifactRegistry(workspace / "artifact_registry.json", "gui-job")
    assert {
        record.artifact_type
        for record in registry.list_records()
    } == {
        "playwright_run_evidence",
        "playwright_trace",
        "playwright_screenshot_manifest",
        "playwright_screenshot",
    }
