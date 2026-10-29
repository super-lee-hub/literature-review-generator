"""Zero-provider planning on the reconciled original R1 typed source inputs."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from runtime.control_plane import ReviewControlPlane
from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import hash_json
from services.artifact_registry import file_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-count", type=int)
    parser.add_argument("--physical-call-limit", type=int)
    args = parser.parse_args()
    manifest = json.loads(args.inputs.read_text(encoding="utf-8"))
    rows = manifest["items"]
    if len(rows) != manifest["authorized_reuse_summary_count"] or len(rows) != 63:
        # This script is the named corpus audit; production code has no 63 cap.
        raise ValueError("the named R1 audit must use all 63 reconciled items")
    summary_paths = [
        str(Path(row["summary_manifest_path"]).expanduser().resolve())
        for row in rows
    ]
    historical_spec_path = Path(manifest["historical_runtime_spec_path"]).expanduser().resolve()
    if file_sha256(historical_spec_path) != manifest["historical_runtime_spec_sha256"]:
        raise ValueError("R1 historical RuntimeJobSpec changed after reconciliation")
    historical_spec = json.loads(historical_spec_path.read_text(encoding="utf-8"))
    historical_source = historical_spec.get("source")
    historical_metadata = historical_spec.get("metadata")
    if not isinstance(historical_source, dict) or not isinstance(historical_metadata, dict):
        raise TypeError("R1 historical RuntimeJobSpec lacks its source or ReviewIntent binding")
    review_intent = historical_metadata.get("review_intent")
    if not isinstance(review_intent, dict):
        raise TypeError("R1 historical RuntimeJobSpec has no ReviewIntent")
    report_path = Path(manifest["source_report_path"]).expanduser().resolve()
    original_report = Path(str(historical_source.get("zotero_report") or "")).expanduser().resolve()
    library_path = Path(str(historical_source.get("library_path") or "")).expanduser().resolve()
    if report_path != original_report or not report_path.is_file() or not library_path.is_dir():
        raise ValueError("R1 original report/library source binding is unavailable")
    if file_sha256(report_path) != manifest["source_report_sha256"]:
        raise ValueError("R1 original source report changed after reconciliation")

    def verify_source_hashes() -> None:
        for row in rows:
            if file_sha256(row["summary_manifest_path"]) != row["summary_manifest_file_sha256"]:
                raise ValueError("R1 source summary authority changed after reconciliation")
            if file_sha256(row["pdf_path"]) != row["actual_pdf_sha256"]:
                raise ValueError("R1 source PDF changed after reconciliation")

    control = ReviewControlPlane(repo_root=_REPO)
    verify_source_hashes()
    config_sha256 = file_sha256(args.config)
    result = control.chunk_plan(
        summary_files=summary_paths,
        job_id="run-first-r1-zero-post-plan", config_path=args.config,
        candidate_count=args.candidate_count, physical_call_limit=args.physical_call_limit,
        review_intent=review_intent,
    )
    verify_source_hashes()
    if file_sha256(report_path) != manifest["source_report_sha256"]:
        raise ValueError("R1 original source report changed during chunk planning")

    prospective_spec = RuntimeJobSpec(
        project_name=str(manifest["project_name"]),
        job_id="run-first-r1-full-stage-provider-free-plan",
        source=RuntimeSourceSpec(
            mode="zotero",
            zotero_report=str(report_path),
            library_path=str(library_path),
        ),
        config=str(args.config.expanduser().resolve()),
        action="run_all",
        reuse_stage1=True,
        reuse_summary_files=tuple(summary_paths),
        # Bind only this source's ReviewIntent. Historical pilot, budget and
        # other execution metadata are deliberately not copied.
        metadata={"review_intent": dict(review_intent)},
    )
    with tempfile.TemporaryDirectory(prefix="run-first-r1-full-stage-plan-") as temp_root:
        spec_path = Path(temp_root) / "prospective-runtime-job-spec.json"
        spec_path.write_text(
            json.dumps(prospective_spec.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        full_stage_result = control.plan(spec_path)
    full_stage_result.pop("spec_path", None)
    full_stage_result["prospective_runtime_spec_sha256"] = hash_json(
        prospective_spec.to_dict()
    )
    if file_sha256(args.config) != config_sha256:
        raise ValueError("R1 plan configuration changed during provider-free planning")
    verify_source_hashes()
    if file_sha256(report_path) != manifest["source_report_sha256"]:
        raise ValueError("R1 original source report changed during full-stage planning")

    semantic_rows = result.get("semantic_request_plan") or ()
    full_stage_request_plan = full_stage_result.get("full_stage_request_plan") or {}
    if not isinstance(full_stage_request_plan, dict):
        full_stage_request_plan = {}
    output_dependent_phases = [
        {
            "stage_name": str(item.get("stage_name") or ""),
            "semantic_role": str(item.get("semantic_role") or ""),
            "reason": str(item.get("reason") or ""),
            "conditional_on": str(item.get("conditional_on") or ""),
            "logical_calls_upper_bound": item.get("logical_calls_upper_bound"),
            "request_materialization": "unknown_until_upstream_output",
        }
        for item in full_stage_request_plan.get("unknown_exposures") or ()
        if isinstance(item, dict)
        and (
            str(item.get("stage_name") or "") in {"review", "validate"}
            or str(item.get("semantic_role") or "") in {"writer", "validator"}
        )
    ]
    graph_config_alignment = (
        "current_config"
        if args.candidate_count is None and args.physical_call_limit is None
        else "chunk_plan_cli_overrides_apply_to_outline_graph_only"
    )
    document = {
        "schema_version": "run-first-r1-zero-post-plan/v1",
        "input_manifest_path": str(args.inputs.resolve()),
        "input_manifest_sha256": file_sha256(args.inputs),
        "config_sha256": config_sha256,
        "provider_posts": 0, "provider_execution_authorized": False,
        "scope": (
            "production Outline chunk-plan plus public full-stage provider-free "
            "projection on the original 63 typed summaries, ReviewIntent, source report, "
            "linked library, current config and hash-verified PDF inventory"
        ),
        "plan": result,
        "full_stage_plan": full_stage_result,
        "semantic_request_graph_reference": {
            "chunk_plan_field": "plan.semantic_request_plan",
            "request_row_count": len(semantic_rows),
            "rows_sha256": hash_json(list(semantic_rows)),
            "topic_request_plan_identity_hash": result.get("topic_request_plan_identity_hash"),
            "upper_bound_status": result.get("semantic_request_upper_bound_status"),
            "preflight_status": result.get("semantic_preflight_status"),
            "config_alignment": graph_config_alignment,
            "provider_posts": 0,
        },
        "future_output_dependent_phases": output_dependent_phases,
        "planning_boundary": {
            "provider_posts": 0,
            "provider_execution_authorized": False,
            "future_writer_validator_payloads_materialized": False,
            "future_writer_validator_note": (
                "Writer and Validator request contents depend on an adopted Outline, "
                "current review catalog and later validation findings."
            ),
        },
    }
    document["content_hash"] = hash_json(document)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "sha256": file_sha256(args.output),
                      "provider_posts": 0, "status": result.get("status"), "keys": list(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
