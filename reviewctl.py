"""Machine-readable Agent control plane entry point.

Use ``python -m reviewctl ...`` from a checkout, or import ``main`` from
tests/host integrations.  All commands emit one JSON object and never print
provider credentials.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from runtime.runner import RuntimeRunnerError
from services.console_io import configure_utf8_stdio, write_ascii_json_line
from services.queue_service import (
    PersistentQueueService,
    QueueError,
    QueueJobSpec,
    QueueRunner,
    QueueState,
    create_queue_job_id,
)


def _queue_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--queue-file", default="output/_queue/queue.json")


def _queue_json_value(raw: str, *, file_path: str = "") -> dict[str, Any]:
    if file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read queue JSON file {file_path}: {exc}") from exc
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"queue JSON is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("queue JSON value must be an object")
    return value


def _validator_host_ack_from_file(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Validator host acknowledgement file is unreadable or invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError("Validator host acknowledgement must be a JSON object")
    return value


def _manual_repair_proposal_from_file(path: str) -> dict[str, Any]:
    """Load a reviewer-authored proposal as one strict, duplicate-free JSON object."""

    try:
        raw_json = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError("manual repair proposal file is unreadable") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"manual repair proposal repeats JSON field {key!r}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"manual repair proposal contains invalid JSON constant {value!r}")

    try:
        value = json.loads(
            raw_json,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"manual repair proposal JSON is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError("manual repair proposal must be a JSON object")
    return value


def _queue_snapshot(service: PersistentQueueService) -> dict[str, Any]:
    jobs = service.list_jobs()
    return {
        "queue_file": str(service.queue_file_path),
        "jobs": [job.to_dict() for job in jobs],
        "runtimes": [
            runtime.to_dict()
            for runtime in service.list_job_runtimes()
        ],
    }


def _queue_terminal_results(
    service: PersistentQueueService,
    job_ids: list[str],
) -> list[dict[str, Any]]:
    """Report each selected job from its durable terminal/runtime record."""

    results: list[dict[str, Any]] = []
    for job_id in job_ids:
        runtime = service.get_job_runtime(job_id)
        if runtime is None:
            results.append({"job_id": job_id, "state": "missing"})
            continue
        results.append(
            {
                "job_id": job_id,
                "state": runtime.state.value,
                "exit_code": (
                    (runtime.result_summary or {}).get("exit_code")
                    if isinstance(runtime.result_summary, dict)
                    else None
                ),
                "error_message": runtime.error_message,
                "result_summary": runtime.result_summary,
            }
        )
    return results


def _queue_run_status(results: list[dict[str, Any]]) -> str:
    if not results:
        return "idle"
    states = {str(item.get("state") or "missing") for item in results}
    if states == {QueueState.COMPLETED.value}:
        return "completed"
    if QueueState.FAILED.value in states or "missing" in states:
        return "failed"
    if QueueState.CANCELLED.value in states or QueueState.CANCEL_ACKNOWLEDGED.value in states:
        return "cancelled"
    return "pending"


def _queue_command(args: argparse.Namespace) -> dict[str, Any]:
    service = PersistentQueueService(args.queue_file)
    command = args.command
    if command == "queue-list":
        state = getattr(args, "state", "")
        jobs = service.list_jobs()
        if state:
            expected = QueueState(state)
            jobs = [
                job
                for job in jobs
                if (runtime := service.get_job_runtime(job.job_id)) is not None
                and runtime.state == expected
            ]
        return {"status": "ok", "command": command, **_queue_snapshot(service), "jobs": [job.to_dict() for job in jobs]}
    if command == "queue-add":
        job_id = str(args.job_id or create_queue_job_id())
        if service.get_job(job_id) is not None:
            raise ValueError(f"queue job already exists: {job_id}")
        parameters = _queue_json_value(args.parameters, file_path=args.parameters_file)
        source_snapshot = _queue_json_value(args.source_snapshot, file_path=args.source_snapshot_file)
        spec = QueueJobSpec(
            job_id=job_id,
            job_type=str(args.job_type),
            project_name=str(args.project_name),
            parameters=parameters,
            depends_on_job_ids=list(args.depends_on or []),
            source_snapshot=source_snapshot,
        )
        service.add_job(spec)
        return {"status": "added", "command": command, "job_id": job_id, **_queue_snapshot(service)}
    if command == "queue-retry":
        if args.job:
            runtime = service.get_job_runtime(args.job)
            if runtime is None or runtime.state not in {QueueState.FAILED, QueueState.CANCELLED}:
                raise ValueError(f"queue job is not retryable: {args.job}")
            if not service.prepare_retry(args.job):
                raise ValueError(f"queue job could not be retried: {args.job}")
            retried = [args.job]
        else:
            retried = service.retry_failed_jobs()
        return {"status": "planned", "command": command, "retried_job_ids": retried, **_queue_snapshot(service)}
    if command == "queue-cancel":
        if not service.request_cancel(args.job, reason=args.reason):
            raise ValueError(f"queue job cannot be cancelled: {args.job}")
        return {"status": "requested", "command": command, "job_id": args.job, **_queue_snapshot(service)}
    if command == "queue-remove":
        if not service.remove_job(args.job):
            raise ValueError(f"queue job was not found: {args.job}")
        return {"status": "removed", "command": command, "job_id": args.job, **_queue_snapshot(service)}
    if command == "queue-export":
        service.save_queue(args.output)
        return {"status": "exported", "command": command, "output": str(args.output), **_queue_snapshot(service)}
    if command == "queue-import":
        service.load_queue(args.input)
        return {"status": "imported", "command": command, "input": str(args.input), **_queue_snapshot(service)}
    if command == "queue-run":
        from services.job_runner import JobRunner

        runner = QueueRunner(service, JobRunner())
        if args.job:
            ran = runner.run_single_job(args.job)
            if not ran:
                raise ValueError(f"queue job cannot be run: {args.job}")
            selected_job_ids = [args.job]
        else:
            selected_job_ids = [
                job.job_id
                for job in (
                    service.list_jobs()
                    if args.all
                    else service.list_jobs_by_state(QueueState.PENDING)
                )
            ]
            runner.run()
        job_results = _queue_terminal_results(service, selected_job_ids)
        return {
            "status": _queue_run_status(job_results),
            "command": command,
            "selected_job_ids": selected_job_ids,
            "ran_job_ids": selected_job_ids,
            "job_results": job_results,
            **_queue_snapshot(service),
        }
    raise ControlPlaneError(f"unsupported queue command: {command}")


def _config_migrate_command(args: argparse.Namespace) -> dict[str, Any]:
    """Explicitly migrate a legacy config file onto the current schema.

    The runtime loader is fail-closed: it rejects unknown keys and sections
    before any legacy handling could run, so this step has to happen before
    validation. It is deliberately *not* run implicitly on every invocation --
    silently rewriting a user's config in the background would be worse than
    refusing to start.
    """

    from services.config_migration import migrate_config_file, migrate_config_text

    path = Path(args.migrate_config)
    if not path.exists():
        return {"status": "failed", "error": f"config file not found: {path}"}

    if args.dry_run:
        # Nothing is written, so the pure transform is enough.
        _migrated, report = migrate_config_text(
            path.read_text(encoding="utf-8"),
            unknown_legacy="drop" if args.drop_unknown_legacy else "preserve",
        )
        backups: list[str] = []
    else:
        report = migrate_config_file(
            path,
            backup=not args.no_backup,
            drop_unknown_legacy=bool(args.drop_unknown_legacy),
        )
        backups = [
            change.split(" ", 2)[-1]
            for change in report.changes
            if change.startswith("wrote backup ")
        ]

    return {
        "status": "ok",
        "config": str(path),
        "changed": report.changed,
        "dry_run": bool(args.dry_run),
        "backup": backups[-1] if backups else None,
        "changes": report.changes,
        "warnings": report.warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewctl")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--config", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--workspace", default="")
    doctor.add_argument("--repo-root", dest="doctor_repo_root", default="")
    doctor.add_argument("--config", dest="doctor_config", default="")

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--config", dest="preflight_config", default="")
    preflight.add_argument("--action", default="analyze")
    preflight.add_argument("--stages", nargs="*", default=None)
    preflight.add_argument("--section", default="")
    preflight.add_argument("--free-mode-enabled", action="store_true")

    micro_probe = subparsers.add_parser("micro-probe")
    micro_probe.add_argument("--config", dest="micro_probe_config", default="")
    micro_probe.add_argument("--action", default="analyze")
    micro_probe.add_argument("--stages", nargs="*", default=None)
    micro_probe.add_argument("--section", default="")
    micro_probe.add_argument("--third-party-acknowledged", action="store_true")
    micro_probe.add_argument("--third-party-host", action="append", default=[])
    micro_probe.add_argument("--free-mode-enabled", action="store_true")

    config_migrate = subparsers.add_parser("config-migrate")
    config_migrate.add_argument("--config", dest="migrate_config", required=True)
    config_migrate.add_argument("--dry-run", action="store_true")
    config_migrate.add_argument("--no-backup", action="store_true")
    config_migrate.add_argument(
        "--drop-unknown-legacy",
        action="store_true",
        help="Discard [API_Parameters] keys with no home in the current schema "
        "(default: preserve them in a marked legacy block)",
    )

    plan = subparsers.add_parser("plan")
    plan.add_argument("--spec", required=True)

    source_revision_plan = subparsers.add_parser(
        "source-revision-plan", help="Save a source-bound derived-run plan without provider calls",
    )
    source_revision_plan.add_argument("--spec", required=True)
    source_revision_plan.add_argument("--parent-workspace", required=True)
    source_revision_plan.add_argument("--output", required=True)

    acceptance_run = subparsers.add_parser("acceptance-run")
    acceptance_run.add_argument("--acceptance-spec", required=True)

    chunk_plan = subparsers.add_parser(
        "chunk-plan",
        help=(
            "Build a provider-free navigation plan and, when --config is supplied, "
            "a route-bound Outline v3 semantic request plan (not an end-to-end Writer/Validator/DOCX budget)"
        ),
    )
    chunk_plan.add_argument("--summary-file", "--summary-source", dest="summary_files", action="append", default=[])
    chunk_plan.add_argument("--job-id", default="chunk-plan")
    chunk_plan.add_argument("--review-intent-file", default="", help="JSON ReviewIntent including exact required comparisons")
    chunk_plan.add_argument("--provider-budget-file", default="", help="Strict V2 prospective run limits for this read-only plan")
    chunk_plan.add_argument("--provider-semantic-contract-version", choices=("source-closed-provider/v2",), default=None)
    chunk_plan.add_argument("--candidate-count", type=int, default=None)
    chunk_plan.add_argument("--physical-call-limit", type=int, default=None)
    chunk_plan.add_argument("--output", default="")
    chunk_plan.add_argument("--json", action="store_true")

    for command in ("run",):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--spec", required=True)
        subparser.add_argument("--job-id", default="")

    source_correction = subparsers.add_parser("source-correction-plan")
    source_correction.add_argument("--workspace", required=True)
    source_correction.add_argument("--proposal", required=True)
    source_correction.add_argument("--output-root", required=True)
    source_correction.add_argument("--json", action="store_true")
    source_correction_inspect = subparsers.add_parser("source-correction-inspect")
    source_correction_inspect.add_argument("--source-workspace", required=True)
    source_correction_inspect.add_argument("--workspace", required=True)
    source_correction_inspect.add_argument("--candidate", required=True)
    source_correction_inspect.add_argument("--json", action="store_true")
    source_correction_adopt = subparsers.add_parser("source-correction-adopt")
    source_correction_adopt.add_argument("--source-workspace", required=True)
    source_correction_adopt.add_argument("--workspace", required=True)
    source_correction_adopt.add_argument("--candidate", required=True)
    source_correction_adopt.add_argument("--expected-hash", required=True)
    source_correction_adopt.add_argument("--actor", required=True)
    source_correction_adopt.add_argument("--reason", required=True)
    source_correction_adopt.add_argument("--json", action="store_true")
    source_correction_reuse = subparsers.add_parser("source-correction-reuse")
    source_correction_reuse.add_argument("--source-workspace", required=True)
    source_correction_reuse.add_argument("--workspace", required=True)
    source_correction_reuse.add_argument("--receipt", required=True)
    source_correction_reuse.add_argument("--json", action="store_true")

    for command in ("status", "inspect", "next-action", "resume", "retry-node", "reconcile", "repair-plan", "repair-apply", "repair-promote", "validate", "validation-status", "cancel", "pause", "adopt"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--job", default="")
        subparser.add_argument("--workspace", default="")
        if command == "retry-node":
            subparser.add_argument("--node", required=True)
        if command == "reconcile":
            subparser.add_argument("--dry-run", action="store_true")
        if command == "repair-apply":
            subparser.add_argument("--plan", required=True)
            subparser.add_argument("--manual-proposal-file", default=None)
            subparser.add_argument("--actor", default="")
            subparser.add_argument("--reason", default="")
        if command == "repair-promote":
            subparser.add_argument("--transaction", required=True)
            subparser.add_argument("--actor", required=True)
            subparser.add_argument("--reason", required=True)
        if command in {"repair-promote", "validate"}:
            subparser.add_argument("--validator-host-ack-file", default="")
        if command == "adopt":
            subparser.add_argument("--artifact", required=True)
            subparser.add_argument("--actor", required=True)
            subparser.add_argument("--reason", required=True)
            subparser.add_argument("--expected-hash", required=True)
        if command in {"cancel", "pause"}:
            subparser.add_argument("--reason", default="user_requested")
        subparser.add_argument("--json", action="store_true", help="Emit JSON output (the default format)")

    downstream = subparsers.add_parser("authorize-downstream")
    downstream.add_argument("--job", default="")
    downstream.add_argument("--workspace", default="")
    downstream.add_argument("--budget-file", required=True)
    downstream.add_argument("--actor", required=True)
    downstream.add_argument("--reason", required=True)
    downstream.add_argument("--authorization-reference", required=True)
    downstream.add_argument("--json", action="store_true")

    export = subparsers.add_parser("export")
    export.add_argument("--batch", default="")
    export.add_argument("--job", default="")
    export.add_argument("--workspace", default="")
    export.add_argument("--json", action="store_true")

    attest = subparsers.add_parser("attest")
    attest.add_argument("--job", default="")
    attest.add_argument("--workspace", default="")
    attest.add_argument("--json", action="store_true")

    queue_list = subparsers.add_parser("queue-list")
    _queue_file_argument(queue_list)
    queue_list.add_argument("--state", choices=[item.value for item in QueueState], default="")
    queue_list.add_argument("--json", action="store_true")

    queue_add = subparsers.add_parser("queue-add")
    _queue_file_argument(queue_add)
    queue_add.add_argument("--job-id", default="")
    queue_add.add_argument("--job-type", default="run")
    queue_add.add_argument("--project-name", required=True)
    queue_add.add_argument("--parameters", default="{}")
    queue_add.add_argument("--parameters-file", default="")
    queue_add.add_argument("--source-snapshot", default="{}")
    queue_add.add_argument("--source-snapshot-file", default="")
    queue_add.add_argument("--depends-on", nargs="*", default=[])
    queue_add.add_argument("--json", action="store_true")

    for command in ("queue-run", "queue-retry", "queue-cancel", "queue-remove"):
        subparser = subparsers.add_parser(command)
        _queue_file_argument(subparser)
        subparser.add_argument("--job", default="")
        if command == "queue-cancel":
            subparser.add_argument("--reason", default="user_requested")
        if command == "queue-run":
            subparser.add_argument("--all", action="store_true")
        subparser.add_argument("--json", action="store_true")

    queue_export = subparsers.add_parser("queue-export")
    _queue_file_argument(queue_export)
    queue_export.add_argument("--output", required=True)
    queue_export.add_argument("--json", action="store_true")

    queue_import = subparsers.add_parser("queue-import")
    _queue_file_argument(queue_import)
    queue_import.add_argument("--input", required=True)
    queue_import.add_argument("--json", action="store_true")
    return parser


def _exit_code(command: str, payload: dict[str, Any]) -> int:
    if command in {"doctor", "preflight", "micro-probe"}:
        return 0 if bool(payload.get("ok")) else 1
    if command == "acceptance-run":
        return 0 if bool(payload.get("ok")) else 1
    if command in {"source-correction-plan", "source-correction-inspect"}:
        return 0 if payload.get("status") == "ready_for_owner_review" else 1
    if command == "source-correction-adopt":
        return 0 if payload.get("status") in {"owner_approved_derived_summary_ready", "already_adopted"} else 1
    if command == "source-correction-reuse":
        return 0 if payload.get("usable_as_stage1_reuse") is True else 1
    if command == "validate":
        return 1 if payload.get("status") == "blocked" else 0
    if command in {"status", "inspect", "next-action", "reconcile", "repair-plan", "validation-status", "attest", "export", "queue-list"}:
        return 0
    if command == "chunk-plan":
        return 0 if payload.get("status") in {"planned", "complete", "available"} else 1
    if command == "config-migrate":
        return 0 if payload.get("status") == "ok" else 1
    if command in {"retry-node", "repair-apply", "repair-promote", "cancel", "pause", "adopt", "queue-add", "queue-run", "queue-retry", "queue-cancel", "queue-remove", "queue-export", "queue-import"}:
        success_statuses = {"available", "complete", "succeeded", "already_adopted", "planned", "requested", "paused", "added", "removed", "exported", "imported", "promoted", "already_promoted"}
        if command == "queue-run":
            success_statuses.update({"completed", "idle"})
        return 0 if payload.get("status") in success_statuses else 1
    if command in {"run", "resume"}:
        return 0 if payload.get("job_status") == "completed" and payload.get("completion_status") == "complete" else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        manual_proposal: dict[str, Any] | None = None
        manual_actor = ""
        manual_reason = ""
        if args.command == "repair-apply":
            has_manual_file = args.manual_proposal_file is not None
            has_manual_identity = bool(str(args.actor or "").strip()) or bool(
                str(args.reason or "").strip()
            )
            if has_manual_file:
                if not str(args.manual_proposal_file).strip():
                    raise ValueError("--manual-proposal-file requires a non-empty path")
                manual_actor = str(args.actor or "")
                manual_reason = str(args.reason or "")
                if not manual_actor.strip() or not manual_reason.strip():
                    raise ValueError(
                        "--manual-proposal-file requires both --actor and --reason"
                    )
                manual_proposal = _manual_repair_proposal_from_file(
                    str(args.manual_proposal_file)
                )
            elif has_manual_identity:
                raise ValueError(
                    "--actor and --reason require --manual-proposal-file"
                )

        # Initialize an owner-supplied acceptance controller at process start so
        # max_wall_seconds covers preprocessing, stage setup, and all later routes,
        # not merely the first ProviderRuntime construction. Validate manual CLI
        # input first so malformed proposals cause no controller or repair side effect.
        from runtime.provider_runtime import provider_budget_controller_from_environment

        provider_budget_controller_from_environment()
        repo_root = args.repo_root or getattr(args, "doctor_repo_root", "")
        control = ReviewControlPlane(repo_root=repo_root or None)
        if args.command.startswith("queue-"):
            payload = _queue_command(args)
        elif args.command == "doctor":
            payload = control.doctor(
                config_path=(getattr(args, "doctor_config", "") or args.config or None),
                workspace=args.workspace or None,
            )
        elif args.command == "preflight":
            payload = control.provider_preflight(
                config_path=(getattr(args, "preflight_config", "") or args.config or None),
                action=args.action,
                requested_stages=args.stages,
                section=args.section or None,
                free_mode_enabled=bool(args.free_mode_enabled),
            )
        elif args.command == "micro-probe":
            payload = control.provider_micro_probe(
                config_path=(getattr(args, "micro_probe_config", "") or args.config or None),
                action=args.action,
                requested_stages=args.stages,
                section=args.section or None,
                third_party_acknowledged=bool(args.third_party_acknowledged),
                third_party_hosts=args.third_party_host,
                free_mode_enabled=bool(args.free_mode_enabled),
            )
        elif args.command == "config-migrate":
            payload = _config_migrate_command(args)
        elif args.command == "plan":
            payload = control.plan(args.spec)
        elif args.command == "source-revision-plan":
            payload = control.source_revision_plan(
                args.spec, parent_workspace=args.parent_workspace, output_path=args.output,
            )
        elif args.command == "acceptance-run":
            payload = control.acceptance_run(args.acceptance_spec)
        elif args.command == "chunk-plan":
            chunk_intent = None
            chunk_budget = None
            if args.review_intent_file:
                chunk_intent = json.loads(Path(args.review_intent_file).read_text(encoding="utf-8"))
                if not isinstance(chunk_intent, dict):
                    raise ValueError("Review intent must be a JSON object")
            if args.provider_budget_file:
                chunk_budget = json.loads(Path(args.provider_budget_file).read_text(encoding="utf-8"))
                if not isinstance(chunk_budget, dict):
                    raise ValueError("Provider budget must be a JSON object")
            payload = control.chunk_plan(
                args.summary_files,
                job_id=args.job_id,
                candidate_count=args.candidate_count,
                physical_call_limit=args.physical_call_limit,
                output_path=args.output or None,
                config_path=args.config or None,
                review_intent=chunk_intent,
                prospective_provider_budget=chunk_budget,
                provider_semantic_contract_version=args.provider_semantic_contract_version,
            )
        elif args.command == "run":
            payload = control.run(
                args.spec,
                job_id=args.job_id,
            )
        elif args.command == "status":
            payload = control.status(job_id=args.job or None, workspace=args.workspace or None)
        elif args.command == "inspect":
            payload = control.inspect(job_id=args.job or None, workspace=args.workspace or None)
        elif args.command == "next-action":
            payload = control.next_action(job_id=args.job or None, workspace=args.workspace or None)
        elif args.command == "authorize-downstream":
            payload = control.authorize_downstream(
                job_id=args.job or None,
                workspace=args.workspace or None,
                budget=_queue_json_value("", file_path=args.budget_file),
                actor=args.actor,
                reason=args.reason,
                authorization_reference=args.authorization_reference,
            )
        elif args.command == "resume":
            payload = control.resume(
                job_id=args.job or None,
                workspace=args.workspace or None,
            )
        elif args.command == "retry-node":
            payload = control.retry_node(
                job_id=args.job or None,
                workspace=args.workspace or None,
                node_id=args.node,
            )
        elif args.command == "reconcile":
            payload = control.reconcile(
                job_id=args.job or None,
                workspace=args.workspace or None,
                dry_run=args.dry_run,
            )
        elif args.command == "source-correction-plan":
            payload = control.source_correction_plan(
                workspace=args.workspace,
                proposal_path=args.proposal,
                output_root=args.output_root,
            )
        elif args.command == "source-correction-inspect":
            payload = control.source_correction_inspect(
                source_workspace=args.source_workspace,
                workspace=args.workspace,
                candidate_artifact_id=args.candidate,
            )
        elif args.command == "source-correction-adopt":
            payload = control.source_correction_adopt(
                source_workspace=args.source_workspace,
                workspace=args.workspace,
                candidate_artifact_id=args.candidate,
                expected_candidate_hash=args.expected_hash,
                actor=args.actor,
                reason=args.reason,
            )
        elif args.command == "source-correction-reuse":
            payload = control.source_correction_reuse(
                source_workspace=args.source_workspace,
                workspace=args.workspace,
                adoption_receipt_artifact_id=args.receipt,
            )
        elif args.command == "repair-plan":
            payload = control.repair_plan(job_id=args.job or None, workspace=args.workspace or None)
        elif args.command == "repair-apply":
            payload = control.repair_apply(
                job_id=args.job or None,
                workspace=args.workspace or None,
                plan_id=args.plan,
                manual_proposal=manual_proposal,
                actor=manual_actor,
                reason=manual_reason,
            )
        elif args.command == "repair-promote":
            payload = control.repair_promote(
                job_id=args.job or None,
                workspace=args.workspace or None,
                transaction_id=args.transaction,
                actor=args.actor,
                reason=args.reason,
                validator_host_acknowledgement=_validator_host_ack_from_file(
                    args.validator_host_ack_file
                ),
            )
        elif args.command == "validate":
            payload = control.validate(
                job_id=args.job or None,
                workspace=args.workspace or None,
                validator_host_acknowledgement=_validator_host_ack_from_file(
                    args.validator_host_ack_file
                ),
            )
        elif args.command == "validation-status":
            payload = control.validation_status(job_id=args.job or None, workspace=args.workspace or None)
        elif args.command == "cancel":
            payload = control.cancel(
                job_id=args.job or None,
                workspace=args.workspace or None,
                reason=args.reason,
            )
        elif args.command == "pause":
            payload = control.pause(
                job_id=args.job or None,
                workspace=args.workspace or None,
                reason=args.reason,
            )
        elif args.command == "adopt":
            payload = control.adopt(
                job_id=args.job or None,
                workspace=args.workspace or None,
                artifact_id=args.artifact,
                actor=args.actor,
                reason=args.reason,
                expected_hash=args.expected_hash,
            )
        elif args.command == "export":
            payload = control.export(
                batch_id=args.batch or None,
                job_id=args.job or None,
                workspace=args.workspace or None,
            )
        elif args.command == "attest":
            payload = control.attest(job_id=args.job or None, workspace=args.workspace or None)
        else:  # pragma: no cover
            raise ControlPlaneError(f"unsupported command: {args.command}")
    except (ControlPlaneError, RuntimeRunnerError, QueueError, OSError, ValueError, TypeError) as exc:
        payload = {
            "control_plane_version": "reviewctl-v1",
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "read_only": True,
        }
        write_ascii_json_line(payload)
        return 2

    write_ascii_json_line(payload)
    return _exit_code(args.command, payload)


if __name__ == "__main__":
    raise SystemExit(main())
