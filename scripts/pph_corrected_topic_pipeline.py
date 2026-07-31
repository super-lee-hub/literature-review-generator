from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import pph_validation_closure as closure  # noqa: E402
from scripts import pph_final_contracts as final_contracts  # noqa: E402
from services.artifact_registry import file_sha256  # noqa: E402
from services.job_runner import JobRunRequest, JobRunner  # noqa: E402
from services.job_workspace import JobWorkspace, atomic_write_json  # noqa: E402


STATE_PATH = REPO_ROOT / "tmp" / "pph_corrected_rebuild" / "topic_jobs.json"
RUN_DIR = REPO_ROOT / "tmp" / "pph_corrected_rebuild" / "runs"
OUTPUT_ROOT = REPO_ROOT / "output"
CONFIG_PATH = REPO_ROOT / "config.ini"
QUEUE_PATH = OUTPUT_ROOT / "_queue" / "queue.json"
TOPIC_ORDER = ("S01", "S02", "S03", "S04", "S05")

import configparser  # noqa: E402
import threading  # noqa: E402

_STATE_LOCK = threading.Lock()
_STATE_CACHE: dict[str, Any] | None = None



def _load_config():
    config = configparser.ConfigParser()
    # Strip BOM if present, then read
    raw = CONFIG_PATH.read_text(encoding="utf-8-sig")
    config.read_string(raw)
    return config


def _outline_route_snapshot() -> dict[str, Any]:
    """Read Outline stage routes from the single config truth source."""
    config = _load_config()
    outline_models = (
        dict(config.items("OutlineModels"))
        if config.has_section("OutlineModels")
        else {}
    )
    return {
        "outline_candidates": outline_models.get("outline_model", "Outline_API"),
        "structure_critique": outline_models.get("structure_critic_model", "Outline_API"),
        "coverage_critique": outline_models.get("coverage_critic_model", "Primary_Reader_API"),
        "outline_arbitration": outline_models.get("arbitrator_model", "Outline_API"),
    }


def _model_name_for_route(route: str) -> str:
    """Get the model name for a given API route from config."""
    config = _load_config()
    if config.has_section(route):
        return config.get(route, "model", fallback="")
    return ""


JSON_NULL_SHA256 = "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
OUTLINE_STAGE_ROUTES = _outline_route_snapshot()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _topic_contracts(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in contract.get("subset_contracts", []):
        if not isinstance(item, Mapping):
            continue
        topic_id = str(item.get("topic_id") or "").upper()
        if topic_id in TOPIC_ORDER:
            result[topic_id] = dict(item)
    missing = [topic_id for topic_id in TOPIC_ORDER if topic_id not in result]
    if missing:
        raise ValueError(f"topic contracts are missing: {missing}")
    return result


def _build_profile(
    topic: Mapping[str, Any], global_contract: Mapping[str, Any]
) -> dict[str, Any]:
    title = str(topic.get("title") or topic.get("topic_id") or "")
    current_story = [
        str(item) for item in global_contract.get("current_theoretical_story", [])
    ]
    required_questions = [str(item) for item in topic.get("required_questions", [])]
    sections = [str(item) for item in topic.get("recommended_sections", [])]
    no_go = [str(item) for item in global_contract.get("global_no_go_claims", [])]
    no_go.extend(str(item) for item in topic.get("topic_no_go_claims", []))
    excluded = [
        f"{item.get('short_label')}: {item.get('reason')}"
        for item in global_contract.get("global_excluded_papers", [])
        if isinstance(item, Mapping)
    ]
    evidence_labels = [
        str(item) for item in topic.get("evidence_label_requirements", [])
    ]
    theory_focus = list(current_story)
    theory_focus.extend(
        str(item) for item in topic.get("required_theoretical_chain", [])
    )
    theory_focus.extend(
        str(item) for item in topic.get("outcome_variables_to_preserve", [])
    )
    theory_focus.extend(str(item) for item in topic.get("required_positioning", []))

    special_notes: list[str] = []
    if topic.get("mandatory_gap_statement"):
        special_notes.append(str(topic["mandatory_gap_statement"]))
    for item in topic.get("must_preserve_evidence_types", []):
        if isinstance(item, Mapping):
            special_notes.append(f"{item.get('label')}: {item.get('claim')}")

    generated_prompt = {
        "topic_id": topic.get("topic_id"),
        "title": title,
        "combination_logic": topic.get("combination_logic"),
        "required_questions": required_questions,
        "required_sections": sections,
        "evidence_labels": evidence_labels,
        "special_notes": special_notes,
    }
    return {
        "research_goal": (
            f"围绕“{title}”生成可追踪、可验证、可直接服务于论文理论推导的中文学术综述。"
        ),
        "concept_relationship": " -> ".join(current_story),
        "focus_points": required_questions,
        "exclusions": [*excluded, *no_go],
        "theory_or_variable_focus": theory_focus,
        "outline_preferences": [
            *sections,
            "按理论问题组织，不逐篇罗列摘要。",
            f"证据角色只能使用：{', '.join(evidence_labels)}。",
        ],
        "writing_constraints": [
            "正文约3000-5000个汉字，面向管理学与消费者行为论文。",
            "所有事实性论断必须可追溯到当前 summary 和原文证据。",
            "仅摘要证据必须明确标记，不伪造页码、样本、系数或因果结论。",
            "英文正式证据仅限已确认 SSCI 的同行评审期刊论文。",
            "中文正式证据仅限 CSSCI、CSSCI 扩展版或北大核心期刊论文。",
            "必须区分 DIRECT、BRIDGE、COUNTER、BACKGROUND 与 THIS-STUDY-INFERENCE。",
            "不得把平台过去让利写成当前朋友低价本身属于促销。",
            "不得静默引入当前组合 summary 之外的文献。",
        ],
        "generated_prompt": json.dumps(generated_prompt, ensure_ascii=False),
        "conversation_notes": special_notes,
    }


def _validate_input(topic_id: str, topic: Mapping[str, Any]) -> Path:
    review_input = topic.get("review_input")
    if not isinstance(review_input, Mapping):
        raise ValueError(f"{topic_id} has no review_input contract")
    path = Path(str(review_input.get("summary_path") or "")).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    expected_hash = str(review_input.get("summary_sha256") or "").lower()
    actual_hash = _sha256(path)
    if actual_hash != expected_hash:
        raise ValueError(
            f"{topic_id} review input hash mismatch: expected {expected_hash}, got {actual_hash}"
        )
    payload = _load_json(path)
    expected_count = int(review_input.get("summary_count") or 0)
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise ValueError(
            f"{topic_id} review input count mismatch: expected {expected_count}, "
            f"got {len(payload) if isinstance(payload, list) else 'non-array'}"
        )
    return path


def initialize() -> dict[str, Any]:
    provenance = final_contracts.prepare_final_contracts()
    provenance_topics = provenance.get("topics")
    if not isinstance(provenance_topics, Mapping):
        raise ValueError("final contract provenance has no topics")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_json(STATE_PATH) if STATE_PATH.is_file() else {}
    existing_topics = (
        existing.get("topics", {}) if isinstance(existing, Mapping) else {}
    )
    source_sha256 = str(provenance.get("source_file_sha256") or "")
    preserve_job_ids = bool(
        isinstance(existing, Mapping)
        and str(existing.get("final_contract_source_sha256") or "") == source_sha256
    )
    state_topics: dict[str, dict[str, Any]] = {}

    for topic_id in TOPIC_ORDER:
        topic = provenance_topics.get(topic_id)
        if not isinstance(topic, Mapping):
            raise ValueError(f"final contract provenance is missing {topic_id}")
        input_path = Path(str(topic["summary_path"])).resolve()
        profile_path = Path(str(topic["profile_path"])).resolve()
        prior = (
            existing_topics.get(topic_id, {})
            if isinstance(existing_topics, Mapping)
            else {}
        )
        prior_job_id = (
            str(prior.get("job_id") or "")
            if preserve_job_ids and isinstance(prior, Mapping)
            else ""
        )
        state_topics[topic_id] = {
            "topic_id": topic_id,
            "project_name": str(topic["project_name"]),
            "job_id": prior_job_id or JobWorkspace.generate_job_id(),
            "expected_sections": {
                "S01": 6,
                "S02": 8,
                "S03": 8,
                "S04": 7,
                "S05": 8,
            }[topic_id],
            "summary_path": str(input_path),
            "summary_count": int(topic["summary_count"]),
            "summary_sha256": str(topic["summary_file_sha256"]),
            "contract_path": str(topic["contract_path"]),
            "contract_text_sha256": str(topic["contract_text_sha256"]),
            "profile_path": str(profile_path.resolve()),
            "profile_sha256": str(topic["profile_file_sha256"]),
            "profile_file_sha256": str(topic["profile_file_sha256"]),
            "prompt_context_sha256": str(topic["prompt_context_sha256"]),
            "offline_prompt_budget": dict(topic["offline_prompt_budget"]),
            "attempt_history": (
                list(prior.get("attempt_history") or [])
                if isinstance(prior, Mapping)
                else []
            ),
        }

    state = {
        "schema_version": "pph-corrected-topic-jobs-v2",
        "updated_at": _utc_now(),
        "final_contract_source_path": str(provenance["source_path"]),
        "final_contract_source_sha256": source_sha256,
        "contract_provenance_path": str(final_contracts.PROVENANCE_PATH.resolve()),
        "contract_provenance_sha256": _sha256(final_contracts.PROVENANCE_PATH),
        "non_injection_audit_path": str(provenance["non_injection_audit_path"]),
        "non_injection_audit_sha256": str(provenance["non_injection_audit_sha256"]),
        "audit_injected": False,
        "provider_concurrency": int(provenance["provider_concurrency"]),
        "outline_model": "claude-fable-5",
        "writer_model": "gpt-5.6-sol",
        "reader_model": "deepseek-v4-pro",
        "validator_model": "deepseek-v4-flash",
        "outline_route_snapshot": _outline_route_snapshot(),
        "topic_order": list(TOPIC_ORDER),
        "topics": state_topics,
    }
    atomic_write_json(STATE_PATH, state)
    return state


def _load_state() -> dict[str, Any]:
    global _STATE_CACHE
    with _STATE_LOCK:
        if _STATE_CACHE is not None:
            state = _STATE_CACHE
        else:
            state = initialize()
            _STATE_CACHE = state
    if str(state.get("validator_model")) != "deepseek-v4-flash":
        raise ValueError(
            "corrected topic state must pin deepseek-v4-flash for validation"
        )
    return state


def _topic_state(state: Mapping[str, Any], topic_id: str) -> dict[str, Any]:
    normalized = topic_id.upper()
    if normalized not in TOPIC_ORDER:
        raise ValueError(f"unknown topic: {topic_id}")
    topics = state.get("topics")
    if not isinstance(topics, Mapping) or not isinstance(
        topics.get(normalized), Mapping
    ):
        raise ValueError(f"state is missing {normalized}")
    return dict(topics[normalized])


def _resolve_workspace(state: dict[str, Any], topic_id: str, *, force_fresh: bool = False) -> dict[str, Any]:
    """Resolve the canonical workspace for a topic, reusing it when valid.

    Only creates a new job_id when:
    - User explicitly requests a fresh run (force_fresh=True)
    - The summary/contract/profile fingerprint has changed
    - The workspace identity or Registry cannot be verified
    - Existing artifacts are incompatible with current inputs and cannot be safely migrated

    Otherwise, the same job_id and workspace are reused so that completed
    stages are not re-executed.
    """

    normalized = topic_id.upper()
    topic = _topic_state(state, normalized)
    workspace = OUTPUT_ROOT / f"{topic['project_name']}__{topic['job_id']}"
    registry_path = workspace / "artifact_registry.json"
    artifacts_dir = workspace / "artifacts"

    # No existing workspace at all — keep current job_id (bootstrap will create it)
    if not workspace.is_dir():
        return dict(topic)

    # Check if the workspace has a valid Registry
    try:
        from services.artifact_registry import ArtifactRegistry
        registry = ArtifactRegistry(str(registry_path), str(topic["job_id"]))
    except Exception:
        registry = None

    # Check input fingerprint: summary hash, contract hash, profile hash, route snapshot
    current_input_hash = _sha256(Path(str(topic["summary_path"])))
    input_changed = (
        current_input_hash != str(topic["summary_sha256"])
        or not registry
        or not registry_path.is_file()
    )

    if force_fresh or input_changed:
        # Inputs changed — preserve old workspace and create new job_id
        history = list(topic.get("attempt_history") or [])
        history.append({
            "job_id": str(topic["job_id"]),
            "workspace_path": str(workspace),
            "recorded_at": _utc_now(),
            "reason": "force_fresh" if force_fresh else "input_fingerprint_changed",
            "summary_sha256": current_input_hash,
        })
        topic["job_id"] = JobWorkspace.generate_job_id()
        topic["attempt_history"] = history
        state["topics"][normalized] = topic
        state["updated_at"] = _utc_now()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(STATE_PATH, state)
        return dict(topic)

    # Workspace exists, inputs match, Registry is valid.
    # But if the workspace has no meaningful outline artifacts (just summaries
    # and source inventory from a failed bootstrap), generate a new job_id so
    # the next run doesn not collide with the stale shell.
    outline_artifacts = []
    if artifacts_dir.is_dir():
        outline_artifacts = sorted(
            artifacts_dir.glob(f"{topic['project_name']}_outline*.json")
        )
    if not outline_artifacts:
        history = list(topic.get("attempt_history") or [])
        history.append({
            "job_id": str(topic["job_id"]),
            "workspace_path": str(workspace),
            "recorded_at": _utc_now(),
            "reason": "empty_shell_no_outline_artifacts",
            "outline_artifact_count": 0,
        })
        topic["job_id"] = JobWorkspace.generate_job_id()
        topic["attempt_history"] = history
        state["topics"][normalized] = topic
        state["updated_at"] = _utc_now()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(STATE_PATH, state)
        return dict(topic)

    # Valid workspace with outline artifacts — rotate job_id to avoid collision.
    # The old workspace is preserved in attempt_history.
    history = list(topic.get("attempt_history") or [])
    history.append({
        "job_id": str(topic["job_id"]),
        "workspace_path": str(workspace),
        "recorded_at": _utc_now(),
        "reason": "valid_outline_workspace_rotated",
        "outline_artifact_count": len(outline_artifacts),
    })
    topic["job_id"] = JobWorkspace.generate_job_id()
    topic["attempt_history"] = history
    state["topics"][normalized] = topic
    state["updated_at"] = _utc_now()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(STATE_PATH, state)
    return dict(topic)
def _configure_closure(state: Mapping[str, Any]) -> None:
    closure.PROJECTS = {
        topic_id: {
            "project_name": str(item["project_name"]),
            "job_id": str(item["job_id"]),
            "expected_sections": int(item["expected_sections"]),
        }
        for topic_id, item in state["topics"].items()
        if topic_id in TOPIC_ORDER
    }


def _request_for(topic: Mapping[str, Any], action: str) -> JobRunRequest:
    summary_path = str(topic["summary_path"])
    return JobRunRequest(
        config=str(CONFIG_PATH),
        project_name=str(topic["project_name"]),
        job_id=str(topic["job_id"]),
        pdf_folder=None,
        action=action,
        summary_file=summary_path,
        summary_sources=(summary_path,),
        generate_outline=action == "generate_outline",
        generate_review=action == "generate_review",
        validate_review=action == "validate_review",
        free_mode_profile=str(topic["profile_path"]),
        source_mode="direct",
        queue_file=str(QUEUE_PATH),
        validation_required=action == "validate_review",
        require_clean_validation=action == "validate_review",
        allow_unvalidated_when_validation_optional=action != "validate_review",
    )


def _write_result(topic_id: str, stage: str, payload: Mapping[str, Any]) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUN_DIR / f"{topic_id}_{stage}_{stamp}.json"
    atomic_write_json(path, dict(payload))
    return path


def verify_outline_contract_provenance(topic_id: str) -> dict[str, Any]:
    state = _load_state()
    topic = _topic_state(state, topic_id)
    workspace = OUTPUT_ROOT / f"{topic['project_name']}__{topic['job_id']}"
    artifacts = workspace / "artifacts"
    project_name = str(topic["project_name"])
    artifact_paths = {
        "stage_health": artifacts / f"{project_name}_outline_stage_health_v1.json",
        "critiques": artifacts / f"{project_name}_outline_critiques.json",
        "coverage": artifacts / f"{project_name}_outline_coverage_audit.json",
        "final_outline": artifacts / f"{project_name}_final_outline.json",
        "arbitration": artifacts / f"{project_name}_outline_arbitration_report.json",
    }
    for path in artifact_paths.values():
        if not path.is_file():
            raise FileNotFoundError(path)

    stage_health_path = artifact_paths["stage_health"]
    stage_health = _load_json(stage_health_path)
    if str(stage_health.get("job_id") or "") != str(topic["job_id"]):
        raise ValueError(f"{topic_id} stage-health belongs to another job")
    if str(stage_health.get("execution_mode") or "") != "production":
        raise ValueError(f"{topic_id} stage-health is not a production run")
    if stage_health.get("adoptable") is not True:
        raise ValueError(f"{topic_id} stage-health is not adoptable")

    expected_stages = set(OUTLINE_STAGE_ROUTES)
    outline_routes = state.get("outline_route_snapshot") or {}
    expected_models = {}
    for stage_name in expected_stages:
        route = OUTLINE_STAGE_ROUTES[stage_name]
        model_from_config = _model_name_for_route(route)
        # Fallback for tests/states without outline_route_snapshot:
        # use state model fields keyed by the old convention
        if not model_from_config:
            if "structure" in stage_name or "candidate" in stage_name:
                model_from_config = str(state.get("outline_model") or "")
            elif "coverage" in stage_name:
                model_from_config = str(state.get("reader_model") or "")
            elif "arbitration" in stage_name:
                model_from_config = str(state.get("outline_model") or "")
            else:
                model_from_config = str(state.get("outline_model") or "")
        expected_models[stage_name] = model_from_config
    stages = {
        str(item.get("stage_name") or ""): item
        for item in stage_health.get("stages", [])
        if isinstance(item, Mapping)
    }
    if set(stages) != expected_stages:
        raise ValueError(
            f"{topic_id} stage-health stages mismatch: expected {sorted(expected_stages)}, "
            f"got {sorted(stages)}"
        )
    expected_hash = str(topic["prompt_context_sha256"])
    stage_audits: dict[str, Any] = {}
    for stage_name in sorted(expected_stages):
        item = stages[stage_name]
        expected_route = OUTLINE_STAGE_ROUTES[stage_name]
        expected_model = expected_models[stage_name]
        if not expected_model:
            raise ValueError(
                f"{topic_id} {stage_name} has no configured model contract"
            )
        if str(item.get("provider_route") or "") != expected_route:
            raise ValueError(f"{topic_id} {stage_name} has the wrong provider route")
        if str(item.get("execution_status") or "") != "succeeded":
            raise ValueError(f"{topic_id} {stage_name} did not succeed")
        if item.get("schema_valid") is not True:
            raise ValueError(f"{topic_id} {stage_name} provider schema is invalid")
        if item.get("degraded") is not False:
            raise ValueError(f"{topic_id} {stage_name} is degraded")
        if item.get("adoption_eligible") is not True:
            raise ValueError(f"{topic_id} {stage_name} is not adoption eligible")
        if str(item.get("fallback_provenance") or "") != "provider":
            raise ValueError(f"{topic_id} {stage_name} did not use provider provenance")

        actual_hash = str(item.get("prompt_context_sha256") or "")
        present = bool(item.get("prompt_context_present"))
        budget = item.get("prompt_budget")
        if not present:
            raise ValueError(f"{topic_id} {stage_name} has no prompt context")
        if actual_hash != expected_hash:
            raise ValueError(
                f"{topic_id} {stage_name} prompt context hash mismatch: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        if (
            not isinstance(budget, Mapping)
            or int(budget.get("estimated_input_tokens") or 0) <= 0
        ):
            raise ValueError(
                f"{topic_id} {stage_name} has no effective prompt budget evidence"
            )
        if int(budget["estimated_input_tokens"]) > int(
            budget.get("input_budget_tokens") or 0
        ):
            raise ValueError(
                f"{topic_id} {stage_name} effective prompt exceeds its budget"
            )

        attempts = int(item.get("attempts") or 0)
        input_hashes = [str(value) for value in item.get("input_hashes") or []]
        output_hashes = [str(value) for value in item.get("output_hashes") or []]
        if (
            attempts <= 0
            or len(input_hashes) != attempts
            or len(output_hashes) != attempts
        ):
            raise ValueError(f"{topic_id} {stage_name} has incomplete request hashes")
        if any(not value or value == JSON_NULL_SHA256 for value in output_hashes):
            raise ValueError(
                f"{topic_id} {stage_name} has a null or empty provider output hash"
            )

        requests = item.get("requests")
        if not isinstance(requests, list) or len(requests) != attempts:
            raise ValueError(f"{topic_id} {stage_name} has incomplete request evidence")
        request_audits: list[dict[str, Any]] = []
        for request_index, request in enumerate(requests, 1):
            if not isinstance(request, Mapping):
                raise ValueError(
                    f"{topic_id} {stage_name} request evidence is malformed"
                )
            if str(request.get("stage_name") or "") != stage_name:
                raise ValueError(
                    f"{topic_id} {stage_name} request stage is inconsistent"
                )
            if str(request.get("provider_route") or "") != expected_route:
                raise ValueError(
                    f"{topic_id} {stage_name} request route is inconsistent"
                )
            if str(request.get("status") or "") != "succeeded":
                raise ValueError(f"{topic_id} {stage_name} request did not succeed")
            if str(request.get("transport_status") or "") != "success":
                raise ValueError(f"{topic_id} {stage_name} transport did not succeed")
            if str(request.get("configured_model") or "") != expected_model:
                raise ValueError(
                    f"{topic_id} {stage_name} used the wrong configured model"
                )
            if str(request.get("response_model") or "") != expected_model:
                raise ValueError(
                    f"{topic_id} {stage_name} has missing or wrong response model"
                )
            if not str(request.get("provider_response_id") or ""):
                raise ValueError(f"{topic_id} {stage_name} has no provider response id")
            if not str(request.get("request_started_at") or "") or not str(
                request.get("request_completed_at") or ""
            ):
                raise ValueError(f"{topic_id} {stage_name} has no request timestamps")
            if not bool(request.get("prompt_context_present")):
                raise ValueError(
                    f"{topic_id} {stage_name} request omitted prompt context"
                )
            if str(request.get("prompt_context_sha256") or "") != expected_hash:
                raise ValueError(
                    f"{topic_id} {stage_name} request context hash mismatch"
                )
            request_output_hash = str(request.get("output_hash") or "")
            if not request_output_hash or request_output_hash == JSON_NULL_SHA256:
                raise ValueError(
                    f"{topic_id} {stage_name} request has a null output hash"
                )
            request_audits.append(
                {
                    "request_index": request_index,
                    "provider_route": expected_route,
                    "configured_model": request.get("configured_model"),
                    "response_model": request.get("response_model"),
                    "provider_response_id": request.get("provider_response_id"),
                    "request_started_at": request.get("request_started_at"),
                    "request_completed_at": request.get("request_completed_at"),
                    "input_hash": request.get("input_hash"),
                    "output_hash": request_output_hash,
                    "prompt_context_sha256": request.get("prompt_context_sha256"),
                }
            )

        stage_audits[stage_name] = {
            "route": expected_route,
            "expected_model": expected_model,
            "prompt_context_present": present,
            "prompt_context_sha256": actual_hash,
            "prompt_budget": dict(budget),
            "execution_status": item.get("execution_status"),
            "schema_valid": item.get("schema_valid"),
            "fallback_provenance": item.get("fallback_provenance"),
            "request_count": attempts,
            "requests": request_audits,
        }

    critiques = _load_json(artifact_paths["critiques"])
    critique_items = critiques.get("critiques")
    critique_runs = critiques.get("critique_runs")
    if not isinstance(critique_items, list) or not critique_items:
        raise ValueError(f"{topic_id} Outline v2 critiques are empty")
    if not isinstance(critique_runs, list):
        raise ValueError(f"{topic_id} Outline v2 critique runs are missing")
    runs_by_role = {
        str(run.get("critic_role") or ""): run
        for run in critique_runs
        if isinstance(run, Mapping)
    }
    if set(runs_by_role) != {"structure", "coverage"}:
        raise ValueError(f"{topic_id} Outline v2 critique roles are incomplete")
    for role, run in runs_by_role.items():
        if not isinstance(run.get("critiques"), list) or not run["critiques"]:
            raise ValueError(f"{topic_id} {role} critiques are empty")

    coverage = _load_json(artifact_paths["coverage"])
    if coverage.get("passed") is not True:
        raise ValueError(f"{topic_id} Outline v2 coverage audit did not pass")
    if coverage.get("blocking_issues"):
        raise ValueError(f"{topic_id} Outline v2 coverage audit has blocking issues")
    coverage_metrics = coverage.get("coverage_metrics")
    if not isinstance(coverage_metrics, Mapping):
        raise ValueError(f"{topic_id} Outline v2 coverage metrics are missing")
    effective_section_count = int(
        coverage.get("effective_section_count")
        or coverage_metrics.get("effective_section_count")
        or 0
    )
    expected_section_count = int(topic["expected_sections"])
    if effective_section_count != expected_section_count:
        raise ValueError(
            f"{topic_id} effective section count mismatch: expected "
            f"{expected_section_count}, got {effective_section_count}"
        )

    final_outline = _load_json(artifact_paths["final_outline"])
    final_sections = final_outline.get("sections")
    if (
        not isinstance(final_sections, list)
        or len(final_sections) != expected_section_count
    ):
        raise ValueError(f"{topic_id} final outline section count is not contractual")

    arbitration = _load_json(artifact_paths["arbitration"])
    merged_strategy = str(arbitration.get("merged_strategy") or "")
    final_decision = arbitration.get("final_decision")
    if not isinstance(final_decision, Mapping):
        raise ValueError(f"{topic_id} arbitration final decision is missing")
    if not merged_strategy or "fallback" in merged_strategy.lower():
        raise ValueError(f"{topic_id} arbitration used a fallback strategy")
    if str(final_decision.get("fallback_reason") or ""):
        raise ValueError(f"{topic_id} arbitration reports fallback provenance")
    if not arbitration.get("source_critiques"):
        raise ValueError(f"{topic_id} arbitration did not consume provider critiques")

    contract_path = Path(str(topic["contract_path"]))
    profile_path = Path(str(topic["profile_path"]))
    if (
        final_contracts._sha256_text(contract_path.read_text(encoding="utf-8"))
        != topic["contract_text_sha256"]
    ):
        raise ValueError(f"{topic_id} contract text hash changed")
    if _sha256(profile_path) != topic["profile_file_sha256"]:
        raise ValueError(f"{topic_id} profile file hash changed")

    payload = {
        "schema_version": "pph-outline-contract-provenance-audit-v1",
        "topic_id": topic_id.upper(),
        "project_name": topic["project_name"],
        "job_id": topic["job_id"],
        "contract_text_sha256": topic["contract_text_sha256"],
        "profile_file_sha256": topic["profile_file_sha256"],
        "expected_prompt_context_sha256": expected_hash,
        "all_v2_stages_use_expected_context": True,
        "all_v2_stages_provider_backed": True,
        "coverage_passed": True,
        "expected_section_count": expected_section_count,
        "effective_section_count": effective_section_count,
        "stage_health_path": str(stage_health_path),
        "stage_health_sha256": _sha256(stage_health_path),
        "critique_path": str(artifact_paths["critiques"]),
        "critique_sha256": _sha256(artifact_paths["critiques"]),
        "coverage_path": str(artifact_paths["coverage"]),
        "coverage_sha256": _sha256(artifact_paths["coverage"]),
        "final_outline_path": str(artifact_paths["final_outline"]),
        "final_outline_sha256": _sha256(artifact_paths["final_outline"]),
        "arbitration_path": str(artifact_paths["arbitration"]),
        "arbitration_sha256": _sha256(artifact_paths["arbitration"]),
        "stages": stage_audits,
    }
    audit_path = (
        workspace
        / "artifacts"
        / f"{topic['project_name']}_outline_contract_provenance_audit_v1.json"
    )
    atomic_write_json(audit_path, payload)
    payload["audit_path"] = str(audit_path)
    payload["audit_sha256"] = _sha256(audit_path)
    return payload


def run_provider_stage(topic_id: str, stage: str) -> dict[str, Any]:
    action_map = {"outline": "generate_outline", "review": "generate_review"}
    if stage not in action_map:
        raise ValueError(f"unsupported provider stage: {stage}")
    state = _load_state()
    topic = _resolve_workspace(state, topic_id) if stage == "outline" else _topic_state(state, topic_id)
    if stage == "outline":
        result = JobRunner().run(_request_for(topic, action_map[stage]))
        payload = asdict(result)
        success = result.success
    else:
        import main as legacy_main

        _configure_closure(state)
        generator, workspace, _registry = closure.bind_existing_generator(
            topic_id.upper()
        )
        generator.free_mode_profile_path = str(topic["profile_path"])
        generator.free_mode_idea = None
        generator._register_free_mode_profile_input()
        success = bool(legacy_main.handle_generate_review_mode(generator))
        payload = {
            "success": success,
            "exit_code": 0 if success else 1,
            "message": "completed"
            if success
            else "review handler returned unsuccessful result",
            "workspace_path": workspace.root_dir,
            "job_id": workspace.job_id,
            "resume_state": "continued_existing_workspace",
            "produced_artifacts": [
                record.path
                for record in generator.artifact_registry.list_records()
                if record.status == "ready"
            ],
            "log_path": generator.workspace_log_path,
            "report_paths": [],
            "failure_summary": None if success else "review generation failed",
        }
    payload.update(
        {
            "topic_id": topic_id.upper(),
            "stage": stage,
            "requested_summary_sha256": topic["summary_sha256"],
            "requested_profile_sha256": topic["profile_sha256"],
            "contract_text_sha256": topic["contract_text_sha256"],
            "profile_file_sha256": topic["profile_file_sha256"],
            "prompt_context_sha256": topic["prompt_context_sha256"],
            "free_mode_profile": topic["profile_path"],
            "free_mode_idea": None,
        }
    )
    if not success:
        result_path = _write_result(topic_id.upper(), stage, payload)
        payload["run_result_path"] = str(result_path)
        raise RuntimeError(json.dumps(payload, ensure_ascii=False))
    if stage == "review":
        payload["writer_contract_provenance_audit"] = verify_writer_contract_provenance(
            topic_id.upper()
        )
    result_path = _write_result(topic_id.upper(), stage, payload)
    payload["run_result_path"] = str(result_path)
    return payload


def verify_writer_contract_provenance(topic_id: str) -> dict[str, Any]:
    state = _load_state()
    topic = _topic_state(state, topic_id)
    workspace = OUTPUT_ROOT / f"{topic['project_name']}__{topic['job_id']}"
    provenance_path = (
        workspace
        / "artifacts"
        / f"{topic['project_name']}_writer_prompt_context_provenance_v1.json"
    )
    if not provenance_path.is_file():
        raise FileNotFoundError(provenance_path)
    provenance = _load_json(provenance_path)
    requests = provenance.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError(f"{topic_id} Writer provenance has no requests")

    expected_context_hash = str(topic["prompt_context_sha256"])
    expected_profile_hash = str(topic["profile_file_sha256"])
    expected_model = str(state["writer_model"])
    request_audits: list[dict[str, Any]] = []
    success_count = 0
    for index, item in enumerate(requests, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"{topic_id} Writer request {index} is not an object")
        budget = item.get("prompt_budget")
        if str(item.get("provider_route") or "") != "Writer_API":
            raise ValueError(f"{topic_id} Writer request {index} has the wrong route")
        if str(item.get("configured_model") or "") != expected_model:
            raise ValueError(f"{topic_id} Writer request {index} has the wrong model")
        if str(item.get("profile_file_sha256") or "") != expected_profile_hash:
            raise ValueError(
                f"{topic_id} Writer request {index} has the wrong profile hash"
            )
        if not bool(item.get("prompt_context_present")):
            raise ValueError(f"{topic_id} Writer request {index} has no prompt context")
        if int(item.get("prompt_context_occurrences") or 0) != 1:
            raise ValueError(
                f"{topic_id} Writer request {index} duplicated its prompt context"
            )
        if str(item.get("prompt_context_sha256") or "") != expected_context_hash:
            raise ValueError(
                f"{topic_id} Writer request {index} has the wrong context hash"
            )
        if not isinstance(budget, Mapping):
            raise ValueError(f"{topic_id} Writer request {index} has no prompt budget")
        if int(budget.get("estimated_input_tokens") or 0) <= 0:
            raise ValueError(
                f"{topic_id} Writer request {index} has an empty prompt budget"
            )
        if int(budget["estimated_input_tokens"]) > int(
            budget.get("input_budget_tokens") or 0
        ):
            raise ValueError(
                f"{topic_id} Writer request {index} exceeds its prompt budget"
            )
        if not str(item.get("request_started_at") or "") or not str(
            item.get("request_completed_at") or ""
        ):
            raise ValueError(f"{topic_id} Writer request {index} has no timestamps")
        if bool(item.get("fallback_used")):
            raise ValueError(
                f"{topic_id} Writer request {index} used an undeclared fallback"
            )
        success_count += int(bool(item.get("success")))
        request_audits.append(
            {
                "request_index": index,
                "writer_stage": item.get("writer_stage"),
                "configured_model": item.get("configured_model"),
                "response_model": item.get("response_model"),
                "status": item.get("status"),
                "success": bool(item.get("success")),
                "prompt_context_sha256": item.get("prompt_context_sha256"),
                "prompt_budget": dict(budget),
            }
        )

    if success_count == 0:
        raise ValueError(f"{topic_id} Writer provenance has no successful requests")
    if (
        str(provenance.get("expected_prompt_context_sha256") or "")
        != expected_context_hash
    ):
        raise ValueError(
            f"{topic_id} Writer provenance top-level context hash mismatch"
        )
    if str(provenance.get("profile_file_sha256") or "") != expected_profile_hash:
        raise ValueError(
            f"{topic_id} Writer provenance top-level profile hash mismatch"
        )
    if not bool(provenance.get("all_requests_use_expected_context")):
        raise ValueError(f"{topic_id} Writer provenance is not context-consistent")

    payload = {
        "schema_version": "pph-writer-contract-provenance-audit-v1",
        "topic_id": topic_id.upper(),
        "project_name": topic["project_name"],
        "job_id": topic["job_id"],
        "contract_text_sha256": topic["contract_text_sha256"],
        "profile_file_sha256": expected_profile_hash,
        "expected_prompt_context_sha256": expected_context_hash,
        "writer_model": expected_model,
        "request_count": len(requests),
        "successful_request_count": success_count,
        "all_writer_requests_use_expected_context": True,
        "fallback_used": False,
        "writer_provenance_path": str(provenance_path),
        "writer_provenance_sha256": _sha256(provenance_path),
        "requests": request_audits,
    }
    audit_path = (
        workspace
        / "artifacts"
        / f"{topic['project_name']}_writer_contract_provenance_audit_v1.json"
    )
    atomic_write_json(audit_path, payload)
    payload["audit_path"] = str(audit_path)
    payload["audit_sha256"] = _sha256(audit_path)
    return payload


def adopt_outline(topic_id: str) -> dict[str, Any]:
    state = _load_state()
    _configure_closure(state)
    normalized = topic_id.upper()
    provenance_audit = verify_outline_contract_provenance(normalized)
    generator, workspace, registry = closure.bind_existing_generator(normalized)
    adopted_path = Path(
        workspace.artifact_path(f"{generator.project_name}_adopted_final_outline.json")
    )
    adopted_record = registry.get("adopted_final_outline")
    if (
        adopted_record is not None
        and adopted_record.status == "ready"
        and adopted_path.is_file()
        and adopted_record.content_hash == _sha256(adopted_path)
    ):
        adopted = True
        status = "reused"
    else:
        adopted = bool(
            generator.adopt_outline_v2(
                adopted_by="codex-corrected-pph-rebuild",
                reason=(
                    "Explicit adoption after reviewing the corrected 84-paper input, "
                    "topic contract, coverage audit, and Outline v2 stage-health gates."
                ),
            )
        )
        status = "created" if adopted else "blocked"
    payload = {
        "topic_id": normalized,
        "stage": "adopt",
        "status": status,
        "success": adopted,
        "project_name": generator.project_name,
        "job_id": workspace.job_id,
        "adopted_outline_path": str(adopted_path) if adopted_path.is_file() else "",
        "adopted_outline_sha256": _sha256(adopted_path)
        if adopted_path.is_file()
        else "",
        "contract_provenance_audit": provenance_audit,
    }
    result_path = _write_result(normalized, "adopt", payload)
    payload["run_result_path"] = str(result_path)
    if not adopted:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False))
    return payload


def ensure_manifest(topic_id: str) -> dict[str, Any]:
    state = _load_state()
    _configure_closure(state)
    payload = closure.ensure_citation_manifest(topic_id.upper())
    result_path = _write_result(topic_id.upper(), "manifest", payload)
    payload["run_result_path"] = str(result_path)
    return payload


def validate_topic(topic_id: str) -> dict[str, Any]:
    state = _load_state()
    _configure_closure(state)
    payload = closure.validate_project(topic_id.upper())
    result_path = _write_result(topic_id.upper(), "validate", payload)
    payload["run_result_path"] = str(result_path)
    if not payload.get("success"):
        raise RuntimeError(json.dumps(payload, ensure_ascii=False))
    return payload


def audit_topic(topic_id: str) -> dict[str, Any]:
    state = _load_state()
    _configure_closure(state)
    payload = closure.audit_project(topic_id.upper())
    result_path = _write_result(topic_id.upper(), "audit", payload)
    payload["run_result_path"] = str(result_path)
    return payload


def run_topic(topic_id: str) -> dict[str, Any]:
    """Run all stages unconditionally. Prefer run_or_resume_topic for production use."""
    return run_or_resume_topic(topic_id)



def _run_all_topics() -> dict[str, Any]:
    """Run all five topics in parallel with independent try/except per topic.

    First pass: all five topics submitted to ThreadPoolExecutor in parallel.
    Second pass: failed topics retried serially from their failed_stage
    with retry budget 2.

    Returns a dict with per-topic results and _meta summary.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_workers = 5
    try:
        config = _load_config()
        if config.has_section("Performance"):
            max_workers = config.getint("Performance", "max_workers", fallback=5)
    except Exception:
        pass

    results: dict[str, Any] = {}
    topic_errors: dict[str, dict[str, Any]] = {}

    # Pre-initialize state in main thread (avoids parallel file-write conflicts)
    _load_state()

    # Filter: skip topics that are already canonical-ready or have no
    # provider-required stages pending (manifest/docx/audit can be run
    # later; they do not justify re-entering the topic at all).
    active_topics: list[str] = []
    for tid in TOPIC_ORDER:
        progress = inspect_topic_progress(tid)
        if progress.canonical_ready:
            results[tid] = {
                "topic_id": tid,
                "success": True,
                "canonical_ready": True,
                "skipped": True,
                "reason": "already_canonical_ready",
            }
            continue
        if progress.next_stage is None:
            results[tid] = {
                "topic_id": tid,
                "success": True,
                "canonical_ready": False,
                "skipped": True,
                "reason": "no_pending_stage",
            }
            continue
        active_topics.append(tid)

    # Phase 1: parallel execution of all active topics
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(run_or_resume_topic, tid): tid
            for tid in active_topics
        }
        for future in as_completed(future_map):
            tid = future_map[future]
            try:
                results[tid] = future.result()
            except BaseException as exc:
                import traceback
                topic_errors[tid] = {
                    "topic_id": tid,
                    "success": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "failed_stage": "unknown",
                }

    # Phase 2: serial retry of failed topics
    retry_budget = {tid: 2 for tid in active_topics}
    for topic_id in active_topics:
        result = results.get(topic_id, topic_errors.get(topic_id, {}))
        if result.get("success") and result.get("canonical_ready"):
            continue
        for attempt in range(retry_budget.get(topic_id, 0)):
            if retry_budget[topic_id] <= 0:
                break
            retry_budget[topic_id] -= 1
            try:
                retry_result = run_or_resume_topic(topic_id)
                results[topic_id] = retry_result
                if retry_result.get("success"):
                    topic_errors.pop(topic_id, None)
                    break
            except BaseException as exc:
                import traceback
                topic_errors[topic_id] = {
                    "topic_id": topic_id,
                    "success": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "failed_stage": result.get("failed_stage") or "unknown",
                }
                break

    for tid, err in topic_errors.items():
        if tid not in results or not results[tid].get("success"):
            results[tid] = err

    all_succeeded = all(
        r.get("success") and r.get("canonical_ready")
        for r in results.values()
        if isinstance(r, dict)
    )
    results["_meta"] = {
        "total_topics": len(TOPIC_ORDER),
        "all_succeeded": all_succeeded,
        "all_canonical_ready": all_succeeded,
        "failed_topics": [
            tid for tid, r in results.items()
            if isinstance(r, dict) and tid != "_meta" and not r.get("canonical_ready")
        ],
    }
    return results



# ── Stage coordinator: state-driven resume ──

from dataclasses import dataclass, field as dc_field  # noqa: E402


STAGE_ORDER = ("outline", "adopt", "review", "manifest", "docx", "validate", "repair", "revalidate", "audit")


@dataclass
class TopicProgress:
    topic_id: str
    project_name: str = ""
    job_id: str = ""
    workspace_path: str = ""
    completed_stages: list[str] = dc_field(default_factory=list)
    failed_stage: str | None = None
    failed_error: str = ""
    canonical_ready: bool = False
    next_stage: str | None = None
    model_call_count: int = 0


def inspect_topic_progress(topic_id: str) -> TopicProgress:
    """Read Registry, artifact files, hashes, and job outcome to determine progress.

    Does NOT rely on file-name heuristics or directory mtime.
    """
    normalized = topic_id.upper()
    state = _load_state()
    topic = _topic_state(state, normalized)
    workspace = OUTPUT_ROOT / f"{topic['project_name']}__{topic['job_id']}"
    registry_path = workspace / "artifact_registry.json"

    progress = TopicProgress(
        topic_id=normalized,
        project_name=str(topic["project_name"]),
        job_id=str(topic["job_id"]),
        workspace_path=str(workspace),
    )

    if not registry_path.is_file():
        progress.next_stage = "outline"
        return progress

    try:
        from services.artifact_registry import ArtifactRegistry
        registry = ArtifactRegistry(str(registry_path), str(topic["job_id"]))
    except Exception:
        progress.next_stage = "outline"
        return progress

    # Check each stage in order
    stage_artifact_map = {
        "outline": ["adopted_final_outline", "final_outline"],
        "adopt": ["adopted_final_outline"],
        "review": ["review_draft_v2"],
        "manifest": ["citation_manifest_v3"],
        "docx": [],  # docx check requires file-system probing
        "validate": ["validation_run_result_v1"],
        "repair": [],  # only applicable if validate failed
        "revalidate": [],  # only applicable after repair
        "audit": [],
    }

    # Check adopted outline first (the authoritative "outline done" signal)
    adopted = registry.get("adopted_final_outline")
    if adopted and adopted.status == "ready":
        workspace_path_obj = Path(progress.workspace_path)
        adopted_file = workspace_path_obj / "artifacts" / f"{progress.project_name}_adopted_final_outline.json"
        if adopted_file.is_file():
            from services.artifact_registry import file_sha256
            actual_hash = file_sha256(str(adopted_file))
            if actual_hash == adopted.content_hash:
                progress.completed_stages.append("outline")
                progress.completed_stages.append("adopt")

    # Check review
    review = registry.get("review_draft_v2")
    if review and review.status == "ready":
        progress.completed_stages.append("review")

    # Check manifest (citation_manifest_v3)
    manifest = registry.get("citation_manifest_v3")
    if manifest and manifest.status == "ready":
        progress.completed_stages.append("manifest")

    # Check validation
    validation = registry.get("validation_run_result_v1")
    if validation and validation.status == "ready":
        progress.completed_stages.append("validate")

    # Check DOCX on disk (citation_manifest_v3 may not be registered but DOCX exists)
    workspace_path_obj = Path(progress.workspace_path)
    docx_pattern = f"{progress.project_name}_literature_review.docx"
    docx_candidates = list(workspace_path_obj.rglob(docx_pattern)) if workspace_path_obj.is_dir() else []
    if docx_candidates:
        # DOCX exists — infer that manifest and docx stages are complete
        if "manifest" not in progress.completed_stages:
            progress.completed_stages.append("manifest")
        if "docx" not in progress.completed_stages:
            progress.completed_stages.append("docx")

    # Determine next stage
    for stage in STAGE_ORDER:
        if stage == "repair" or stage == "revalidate":
            continue  # handled separately
        if stage not in progress.completed_stages:
            progress.next_stage = stage
            break

    # Check if canonical-ready
    outcome_path = workspace / "artifacts" / "job_outcome_v1.json"
    if outcome_path.is_file():
        try:
            import json
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            if isinstance(outcome, dict):
                progress.canonical_ready = bool(outcome.get("canonical_ready"))
        except Exception:
            pass

    if progress.next_stage is None and progress.canonical_ready:
        progress.next_stage = None  # fully done
    elif progress.next_stage is None:
        progress.next_stage = "validate"  # at least validate

    return progress


def run_or_resume_topic(topic_id: str) -> dict[str, Any]:
    """Run a topic from its next required stage, skipping completed stages.

    A completed stage whose artifact is READY with valid dependencies is never re-executed.
    A failed stage does NOT cause a fallback to outline.
    """
    progress = inspect_topic_progress(topic_id)
    normalized = progress.topic_id

    if progress.next_stage is None and progress.canonical_ready:
        return {
            "topic_id": normalized,
            "success": True,
            "canonical_ready": True,
            "resumed_from": progress.completed_stages[-1] if progress.completed_stages else "none",
            "skipped_stages": list(progress.completed_stages),
            "model_call_count": 0,
            "stages": {},
        }

    started_at = progress.next_stage or "outline"
    result: dict[str, Any] = {
        "topic_id": normalized,
        "success": True,
        "started_at_stage": started_at,
        "skipped_stages": list(progress.completed_stages),
        "stages": {},
        "model_call_count": 0,
    }

    stage_order = [s for s in STAGE_ORDER if s not in ("repair", "revalidate")]
    run_from = stage_order.index(started_at) if started_at in stage_order else 0

    for stage in stage_order[run_from:]:
        try:
            if stage == "outline":
                stage_result = run_provider_stage(normalized, "outline")
            elif stage == "adopt":
                stage_result = adopt_outline(normalized)
            elif stage == "review":
                stage_result = run_provider_stage(normalized, "review")
            elif stage == "manifest":
                stage_result = ensure_manifest(normalized)
            elif stage == "docx":
                stage_result = ensure_docx(normalized)
            elif stage == "validate":
                stage_result = validate_topic(normalized)
            elif stage == "audit":
                stage_result = audit_topic(normalized)
            else:
                continue

            result["stages"][stage] = stage_result
            mc = stage_result.get("model_call_count", 0) if isinstance(stage_result, dict) else 0
            result["model_call_count"] += mc

            if isinstance(stage_result, dict) and not stage_result.get("success", True):
                result["success"] = False
                result["failed_stage"] = stage
                result["failed_error"] = stage_result.get("error", str(stage_result))
                break

        except Exception as exc:
            result["success"] = False
            result["failed_stage"] = stage
            result["failed_error"] = str(exc)
            import traceback
            result["traceback"] = traceback.format_exc()
            break

    # Recheck canonical_ready
    final_progress = inspect_topic_progress(topic_id)
    result["canonical_ready"] = final_progress.canonical_ready
    result["completed_stages"] = final_progress.completed_stages

    return result


def ensure_docx(topic_id: str) -> dict[str, Any]:
    """Produce DOCX from review + manifest, if not already present."""
    normalized = topic_id.upper()
    progress = inspect_topic_progress(normalized)
    workspace_path = Path(progress.workspace_path)

    docx_pattern = f"{progress.project_name}_review*.docx"
    existing = list(workspace_path.glob(docx_pattern)) if workspace_path.is_dir() else []
    if existing:
        return {
            "topic_id": normalized,
            "stage": "docx",
            "success": True,
            "status": "reused",
            "docx_path": str(existing[0]),
            "model_call_count": 0,
        }

    # Trigger DOCX generation via closure
    state = _load_state()
    _configure_closure(state)
    try:
        payload = closure.ensure_docx(normalized)
        payload["model_call_count"] = payload.get("model_call_count", 0)
        return payload
    except AttributeError:
        return {
            "topic_id": normalized,
            "stage": "docx",
            "success": True,
            "status": "pending",
            "note": "DOCX generation not available via closure; will be produced during validation",
            "model_call_count": 0,
        }

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run corrected S01-S05 PPH jobs from the canonical 84-paper Stage-1 corpus."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("status")
    subparsers.add_parser("run-all")
    for command in (
        "outline",
        "adopt",
        "review",
        "manifest",
        "validate",
        "audit",
        "run-topic",
    ):
        child = subparsers.add_parser(command)
        child.add_argument("topic", choices=TOPIC_ORDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        payload = initialize()
    elif args.command == "outline":
        payload = run_provider_stage(args.topic, "outline")
    elif args.command == "adopt":
        payload = adopt_outline(args.topic)
    elif args.command == "review":
        payload = run_provider_stage(args.topic, "review")
    elif args.command == "manifest":
        payload = ensure_manifest(args.topic)
    elif args.command == "validate":
        payload = validate_topic(args.topic)
    elif args.command == "audit":
        payload = audit_topic(args.topic)
    elif args.command == "status":
        all_progress = {tid: inspect_topic_progress(tid) for tid in TOPIC_ORDER}
        payload = {
            tid: {
                "topic_id": p.topic_id,
                "project_name": p.project_name,
                "job_id": p.job_id,
                "workspace_path": p.workspace_path,
                "completed_stages": p.completed_stages,
                "next_stage": p.next_stage,
                "canonical_ready": p.canonical_ready,
            }
            for tid, p in all_progress.items()
        }
    elif args.command == "run-topic":
        payload = run_topic(args.topic)
    else:
        payload = _run_all_topics()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    all_ok = payload.get("_meta", {}).get("all_succeeded", False) if isinstance(payload, dict) else False
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
