from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config_loader import load_config  # noqa: E402
from free_mode.profile_manager import DEFAULT_PROFILE, build_profile_context  # noqa: E402
from outline.arbitration_v2 import _arbitration_prompt  # noqa: E402
from outline.candidates import _candidate_prompt, generate_candidates_deterministic  # noqa: E402
from outline.critique_v2 import (  # noqa: E402
    _critique_prompt,
    build_critiques_v2,
    run_coverage_critique_deterministic,
    run_structure_critique_deterministic,
)
from outline.literature_map import build_literature_map  # noqa: E402
from outline.prompt_budget import PromptBudgetV1  # noqa: E402
from outline.synthesis_flow import build_synthesis_flow  # noqa: E402


SOURCE_PATH = Path(
    r"C:\Users\12130\.codex\attachments\88630fc9-5ab8-46db-ae69-df84672ab9c0\pasted-text.txt"
)
ARTIFACT_ROOT = REPO_ROOT / "output" / "pph_review_work" / "final_topic_contracts"
CONTRACT_DIR = ARTIFACT_ROOT / "contracts"
PROFILE_DIR = ARTIFACT_ROOT / "profiles"
AUDIT_PATH = ARTIFACT_ROOT / "non_injection_audit.md"
PROVENANCE_PATH = ARTIFACT_ROOT / "contract_profile_provenance.json"
CONFIG_PATH = REPO_ROOT / "config.ini"
TOPIC_ORDER = ("S01", "S02", "S03", "S04", "S05")
PROJECTS = {
    "S01": "pph_s01_dynamic_disadvantage",
    "S02": "pph_s02_prior_concession",
    "S03": "pph_s03_concession_to_unfairness",
    "S04": "pph_s04_unfairness_continuance",
    "S05": "pph_s05_subjective_knowledge",
}
SUMMARY_INPUTS = {
    topic_id: REPO_ROOT
    / "output"
    / "pph_review_work"
    / "corrected_stage1_84"
    / "review_inputs"
    / f"{topic_id}_review_input_summaries.json"
    for topic_id in TOPIC_ORDER
}
SCHEMA_GUARD = (
    "These instructions constrain research scope, theory, evidence boundaries, and "
    "coverage only. They do not change the current call's role, strict JSON schema, "
    "field requirements, or output format. Apply them within the current role and "
    "return only the format already required by the surrounding system prompt."
)
AUDIT_MARKER = "## 非注入审计表"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )


def extract_contracts(source_text: str) -> tuple[dict[str, str], str]:
    contracts: dict[str, str] = {}
    for topic_id in TOPIC_ORDER:
        pattern = re.compile(
            rf"^===== {topic_id}_CONTRACT =====\s*$\n"
            rf"(?P<body>.*?)"
            rf"^===== END {topic_id}_CONTRACT =====\s*$",
            flags=re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(source_text)
        if match is None:
            raise ValueError(f"missing final contract markers for {topic_id}")
        body = match.group("body").strip("\r\n")
        if not body:
            raise ValueError(f"{topic_id} contract body is empty")
        contracts[topic_id] = body

    audit_index = source_text.find(AUDIT_MARKER)
    if audit_index < 0:
        raise ValueError("non-injection audit table is missing")
    audit_text = source_text[audit_index:].strip("\r\n")
    return contracts, audit_text


def build_contract_profile(contract_text: str) -> dict[str, Any]:
    profile = dict(DEFAULT_PROFILE)
    profile["generated_prompt"] = contract_text
    return profile


def _route_budget(config: Mapping[str, Mapping[str, Any]], route: str) -> PromptBudgetV1:
    defaults = {
        "Outline_API": ("outline_max_tokens", 16000),
        "Writer_API": ("writer_max_tokens", 32000),
        "Primary_Reader_API": ("primary_max_tokens", 5000),
    }
    api_section = dict(config.get(route, {}))
    api_parameters = dict(config.get("API_Parameters", {}))
    max_key, default_output = defaults[route]
    return PromptBudgetV1(
        model_context_limit=int(api_section.get("max_context_tokens") or 200000),
        max_output_tokens=int(api_section.get("max_tokens") or api_parameters.get(max_key) or default_output),
    )


def _offline_prompt_budget(
    *,
    topic_id: str,
    summaries: Sequence[dict[str, Any]],
    prompt_context: str,
    config: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    literature_map = build_literature_map(summaries, f"offline-{topic_id}")
    synthesis_flow = build_synthesis_flow(literature_map, f"offline-{topic_id}")
    candidates = generate_candidates_deterministic(
        literature_map,
        synthesis_flow,
        3,
        "Outline_API",
        f"offline-{topic_id}",
    )
    structure = run_structure_critique_deterministic(candidates, "Writer_API")
    coverage = run_coverage_critique_deterministic(candidates, "Primary_Reader_API")
    critiques = build_critiques_v2(
        structure,
        coverage,
        [candidate.candidate_id for candidate in candidates.candidates],
    )
    prompts = {
        "outline_candidates": (
            "Outline_API",
            _candidate_prompt(
                literature_map,
                synthesis_flow,
                1,
                summaries,
                strategy_offset=0,
            ),
        ),
        "structure_critique": (
            "Writer_API",
            _critique_prompt(candidates, "structure"),
        ),
        "coverage_critique": (
            "Primary_Reader_API",
            _critique_prompt(candidates, "coverage"),
        ),
        "outline_arbitration": (
            "Outline_API",
            _arbitration_prompt(candidates, critiques),
        ),
    }
    result: dict[str, Any] = {}
    for stage, (route, base_prompt) in prompts.items():
        effective_prompt = prompt_context + base_prompt
        budget = _route_budget(config, route)
        budget.assert_fits(effective_prompt, stage=f"offline_{topic_id}_{stage}")
        result[stage] = {
            "route": route,
            "base_prompt_sha256": _sha256_text(base_prompt),
            "effective_prompt_sha256": _sha256_text(effective_prompt),
            "prompt_context_sha256": _sha256_text(prompt_context),
            "prompt_context_present": bool(prompt_context),
            "prompt_budget": budget.metadata(effective_prompt),
        }
    return result


def prepare_final_contracts() -> dict[str, Any]:
    source_text = SOURCE_PATH.read_text(encoding="utf-8-sig")
    contracts, audit_text = extract_contracts(source_text)
    config = load_config(str(CONFIG_PATH))
    source_sha256 = _sha256_file(SOURCE_PATH)

    CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(AUDIT_PATH, audit_text)

    topics: dict[str, Any] = {}
    for topic_id in TOPIC_ORDER:
        contract_text = contracts[topic_id]
        if not contract_text.startswith(SCHEMA_GUARD):
            raise ValueError(f"{topic_id} does not begin with the fixed schema guard")
        if AUDIT_MARKER in contract_text:
            raise ValueError(f"{topic_id} contract contains the non-injection audit marker")

        contract_path = CONTRACT_DIR / f"{topic_id}_CONTRACT.txt"
        profile_path = PROFILE_DIR / f"{topic_id}_profile.json"
        _atomic_write_text(contract_path, contract_text)
        profile = build_contract_profile(contract_text)
        _atomic_write_json(profile_path, profile)
        profile_readback = json.loads(profile_path.read_text(encoding="utf-8"))
        if profile_readback.get("generated_prompt") != contract_text:
            raise ValueError(f"{topic_id} profile generated_prompt differs from contract text")
        for key, empty_value in DEFAULT_PROFILE.items():
            if key == "generated_prompt":
                continue
            if profile_readback.get(key) != empty_value:
                raise ValueError(f"{topic_id} profile field {key} is not empty")

        other_contracts = [
            other_text
            for other_id, other_text in contracts.items()
            if other_id != topic_id
        ]
        if any(other_text in profile_readback["generated_prompt"] for other_text in other_contracts):
            raise ValueError(f"{topic_id} profile contains another complete topic contract")
        if audit_text in profile_readback["generated_prompt"] or AUDIT_MARKER in profile_readback["generated_prompt"]:
            raise ValueError(f"{topic_id} profile contains the non-injection audit")

        prompt_context = build_profile_context(profile_readback)
        summaries_path = SUMMARY_INPUTS[topic_id]
        summaries = json.loads(summaries_path.read_text(encoding="utf-8"))
        if not isinstance(summaries, list) or not summaries:
            raise ValueError(f"{topic_id} corrected summary input is empty or invalid")
        token_budget = _offline_prompt_budget(
            topic_id=topic_id,
            summaries=summaries,
            prompt_context=prompt_context,
            config=config,
        )
        topics[topic_id] = {
            "project_name": PROJECTS[topic_id],
            "contract_path": str(contract_path.resolve()),
            "contract_text_sha256": _sha256_text(contract_text),
            "contract_file_sha256": _sha256_file(contract_path),
            "profile_path": str(profile_path.resolve()),
            "profile_file_sha256": _sha256_file(profile_path),
            "prompt_context_sha256": _sha256_text(prompt_context),
            "prompt_context_present": bool(prompt_context),
            "summary_path": str(summaries_path.resolve()),
            "summary_file_sha256": _sha256_file(summaries_path),
            "summary_count": len(summaries),
            "offline_prompt_budget": token_budget,
        }

    provenance = {
        "schema_version": "pph-final-topic-contract-provenance-v1",
        "source_path": str(SOURCE_PATH),
        "source_file_sha256": source_sha256,
        "non_injection_audit_path": str(AUDIT_PATH.resolve()),
        "non_injection_audit_sha256": _sha256_file(AUDIT_PATH),
        "audit_injected": False,
        "profile_and_idea_mutually_exclusive": True,
        "provider_concurrency": 1,
        "provider_concurrency_reason": (
            "Preflight measured sustained 100% CPU and 1.32 GB free memory (8.36%); "
            "topics and provider calls remain sequential until resources recover."
        ),
        "topics": topics,
    }
    _atomic_write_json(PROVENANCE_PATH, provenance)
    return provenance


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract and validate final S01-S05 contracts.")
    parser.add_argument("command", choices=("prepare", "audit"))
    args = parser.parse_args(argv)
    if args.command == "prepare":
        payload = prepare_final_contracts()
    else:
        payload = prepare_final_contracts()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
