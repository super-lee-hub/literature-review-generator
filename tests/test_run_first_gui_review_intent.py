from __future__ import annotations

import configparser
import importlib
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from outline.v3_models import ReviewIntent
from runtime.runner import AgentRuntimeRunner, RuntimeExecutionResult
from services.job_runner import JobRunner
from services.queue_service import QueueRunner

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def gui_app_module(monkeypatch: pytest.MonkeyPatch):
    fake_ui = types.SimpleNamespace(notify=lambda *_args, **_kwargs: None)
    fake_nicegui = types.ModuleType("nicegui")
    fake_nicegui.ui = fake_ui
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    sys.modules.pop("gui.app", None)
    module = importlib.import_module("gui.app")
    return importlib.reload(module)


def _write_runtime_config(tmp_path: Path) -> Path:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(REPO_ROOT / "config.ini.example", encoding="utf-8")
    parser["Paths"]["output_path"] = str(tmp_path / "output")
    for section in parser.sections():
        if section.endswith("_API"):
            parser[section]["api_key"] = "local-fixture-only"
            hostname = re.sub(r"[^a-z0-9-]", "-", section.casefold().replace("_api", ""))
            parser[section]["api_base"] = f"https://{hostname}.example.test/v1"
    parser["Preprocess"]["parser_mode"] = "local"
    parser["Preprocess"]["primary_parser"] = "local"
    parser["Preprocess"]["fallback_parser"] = "local"
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as stream:
        parser.write(stream)
    return config_path


def _controller(gui_app_module, tmp_path: Path):
    config_path = _write_runtime_config(tmp_path)
    controller = gui_app_module.WorkspaceController(str(config_path))
    pdf_folder = tmp_path / "papers"
    pdf_folder.mkdir()
    (pdf_folder / "paper.pdf").write_bytes(b"%PDF-1.4\nreview intent GUI fixture\n")
    controller.state["workflow"].update(
        {
            "project_name": "gui-review-intent",
            "input_mode": "pdf",
            "work_mode": "normal",
            "pdf_folder": str(pdf_folder),
        }
    )
    # Model the already-acknowledged route hosts in the existing GUI flow.
    for section_name in controller.api_cards:
        controller.third_party_gateway_acknowledged[section_name] = (
            controller._third_party_gateway_fingerprint(section_name)
        )
    return controller, pdf_folder


def _intent_payload() -> dict[str, Any]:
    return {
        "review_question": "Compare the sources under their reported conditions.",
        "required_comparisons": [
            {
                "paper_keys": ["paper-b", "paper-a"],
                "question": "Does the reported mechanism differ across these sources?",
                "relation_type": "mechanism_contrast",
            }
        ],
    }


def test_gui_queue_builder_and_runner_preserve_review_intent_file_metadata(
    gui_app_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, pdf_folder = _controller(gui_app_module, tmp_path)
    intent_file = tmp_path / "review-intent.json"
    intent_file.write_text(json.dumps(_intent_payload()), encoding="utf-8")
    controller.state["workflow"]["review_intent_file"] = str(intent_file)

    job_id = controller.add_job_to_queue(
        "gui-review-intent",
        str(pdf_folder),
        "",
        "run_all",
        input_mode="pdf",
        work_mode="normal",
    )
    assert job_id
    queue = controller._queue_service
    assert queue is not None
    frozen_job = queue.get_job(job_id)
    assert frozen_job is not None
    frozen_spec = frozen_job.execution_snapshot["runtime_spec"]

    expected_intent = ReviewIntent.from_dict(_intent_payload()).to_dict()
    expected_acknowledgement = frozen_spec["metadata"]["external_host_acknowledgement"]
    assert frozen_job.parameters["run_all"] is True
    assert frozen_spec["action"] == "run_all"
    assert frozen_spec["source"]["pdf_folder"] == str(pdf_folder.resolve())
    assert frozen_spec["metadata"]["review_intent"] == expected_intent
    assert frozen_spec["metadata"]["review_intent"]["required_comparisons"][0]["paper_keys"] == [
        "paper-a",
        "paper-b",
    ]

    captured_specs = []

    def capture_after_runtime_validation(
        runner: AgentRuntimeRunner,
        *,
        resume: bool,
    ) -> RuntimeExecutionResult:
        normalized = runner._normalized_spec(resume=resume)
        captured_specs.append(normalized)
        return RuntimeExecutionResult(
            job_id=str(normalized.job_id),
            workspace_path=str(normalized.workspace_path),
            job_status="completed",
            job_disposition="completed",
            canonical_ready=True,
            requires_attention=False,
            attempt_number=1,
            resumed_from_attempt=None,
            completed_stages=(),
            failed_stage=None,
            job_outcome_path="",
            message="GUI ReviewIntent bridge fixture",
            completion_status="completed",
        )

    monkeypatch.setattr(AgentRuntimeRunner, "_execute", capture_after_runtime_validation)
    assert QueueRunner(queue, JobRunner()).run_single_job(job_id) is True
    assert len(captured_specs) == 1
    assert captured_specs[0].metadata["review_intent"] == expected_intent
    assert captured_specs[0].metadata["external_host_acknowledgement"] == expected_acknowledgement
    assert queue.get_job_runtime(job_id).state.value == "completed"


@pytest.mark.parametrize(
    "raw_json, expected_message",
    [
        ("{not-json", "syntax error"),
        (json.dumps({"required_comparisons": [{"paper_keys": ["paper-a", "paper-b", "paper-c"], "question": "Compare"}]}), "exactly two"),
        (json.dumps({"required_comparisons": [{"paper_keys": ["paper-a", "paper-b"], "question": "Compare", "invented_conclusion": "A wins"}]}), "unknown fields"),
        (json.dumps({"required_comparisons": [{"paper_keys": ["paper-a", "paper-b"], "question": "  "}]}), "non-empty string"),
        (json.dumps({"artifact_version": "future-version"}), "fixed by the current schema"),
    ],
)
def test_invalid_review_intent_is_visible_and_stops_before_queueing(
    gui_app_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_json: str,
    expected_message: str,
) -> None:
    controller, _pdf_folder = _controller(gui_app_module, tmp_path)
    controller.state["workflow"]["review_intent_json"] = raw_json
    notifications: list[str] = []
    monkeypatch.setattr(controller, "notify", lambda message, **_kwargs: notifications.append(str(message)))

    assert controller.validate_workflow_request("run_all") is False
    assert notifications and expected_message in notifications[-1]
    assert controller._queue_service is not None
    assert controller._queue_service.list_jobs() == []


def test_gui_review_intent_defaults_and_rejects_ambiguous_sources(gui_app_module, tmp_path: Path) -> None:
    controller, pdf_folder = _controller(gui_app_module, tmp_path)
    default_spec = controller._build_queue_job_spec(
        "gui-review-intent",
        str(pdf_folder),
        "",
        "generate_outline",
        input_mode="pdf",
        work_mode="normal",
    )
    assert "review_intent" not in default_spec.parameters["metadata"]

    controller.state["workflow"]["review_intent_json"] = json.dumps(_intent_payload())
    controller.state["workflow"]["review_intent_file"] = str(tmp_path / "other.json")
    with pytest.raises(ValueError, match="not both"):
        controller._build_queue_job_spec(
            "gui-review-intent",
            str(pdf_folder),
            "",
            "generate_outline",
            input_mode="pdf",
            work_mode="normal",
        )


def test_free_mode_rejects_manual_intent_that_runner_would_override(gui_app_module, tmp_path: Path) -> None:
    controller, pdf_folder = _controller(gui_app_module, tmp_path)
    controller.state["workflow"]["review_intent_json"] = json.dumps(_intent_payload())
    with pytest.raises(ValueError, match="Free Mode supplies its own review intent"):
        controller._build_queue_job_spec(
            "gui-review-intent",
            str(pdf_folder),
            "",
            "run_all",
            input_mode="pdf",
            work_mode="free",
        )


def test_review_intent_form_exposes_inline_json_and_file_picker(gui_app_module, monkeypatch) -> None:
    class Element:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.bound = None
            self.props_calls: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def classes(self, *_args):
            return self

        def bind_value(self, target, key):
            self.bound = (target, key)
            return self

        def props(self, value):
            self.props_calls.append(value)
            return self

    class FakeUi:
        def __init__(self):
            self.textarea_element = None

        def expansion(self, *_args, **_kwargs):
            return Element()

        def card(self):
            return Element()

        def label(self, *_args, **_kwargs):
            return Element()

        def textarea(self, *_args, **kwargs):
            self.textarea_element = Element(**kwargs)
            return self.textarea_element

    fake_ui = FakeUi()
    monkeypatch.setattr(gui_app_module, "ui", fake_ui)
    path_calls = []
    monkeypatch.setattr(
        gui_app_module,
        "_render_path_field",
        lambda _controller, **kwargs: path_calls.append(kwargs),
    )
    workflow = {"review_intent_json": "", "review_intent_file": ""}
    controller = types.SimpleNamespace(t=lambda value: value, state={"workflow": workflow})

    gui_app_module._render_workflow_review_intent_card(controller)

    assert fake_ui.textarea_element is not None
    assert fake_ui.textarea_element.bound == (workflow, "review_intent_json")
    assert any("data-testid=workflow-review-intent-json" in prop for prop in fake_ui.textarea_element.props_calls)
    assert path_calls == [
        {
            "label": "ReviewIntent JSON 文件（可选）",
            "section": "workflow",
            "key": "review_intent_file",
            "pick": "file",
            "title": "选择 ReviewIntent JSON 文件",
            "filetypes": [("JSON Files", "*.json"), ("All Files", "*.*")],
            "test_id": "workflow-review-intent-file",
        }
    ]
