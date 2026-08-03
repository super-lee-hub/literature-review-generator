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
from services.console_io import configure_utf8_stdio, write_ascii_json_line
from services.queue_service import (
    PersistentQueueService,
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
            service.reset_job(args.job)
            service.increment_retry_count(args.job)
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
            ran_job_ids = [args.job]
        else:
            runner.run()
            ran_job_ids = [job.job_id for job in service.list_jobs()]
        return {"status": "completed", "command": command, "ran_job_ids": ran_job_ids, **_queue_snapshot(service)}
    raise ControlPlaneError(f"unsupported queue command: {command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewctl")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--config", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--workspace", default="")
    doctor.add_argument("--repo-root", dest="doctor_repo_root", default="")
    doctor.add_argument("--config", dest="doctor_config", default="")

    plan = subparsers.add_parser("plan")
    plan.add_argument("--spec", required=True)

    for command in ("run",):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--spec", required=True)
        subparser.add_argument("--job-id", default="")

    for command in ("status", "inspect", "next-action", "resume", "retry-node", "reconcile", "repair-plan", "repair-apply", "validate", "cancel", "adopt"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--job", default="")
        subparser.add_argument("--workspace", default="")
        if command == "retry-node":
            subparser.add_argument("--node", required=True)
        if command == "reconcile":
            subparser.add_argument("--dry-run", action="store_true")
        if command == "repair-apply":
            subparser.add_argument("--plan", required=True)
        if command == "adopt":
            subparser.add_argument("--artifact", required=True)
            subparser.add_argument("--actor", default="reviewctl")
        if command == "cancel":
            subparser.add_argument("--reason", default="user_requested")
        subparser.add_argument("--json", action="store_true", help="Emit JSON output (the default format)")

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
    if command == "doctor":
        return 0 if bool(payload.get("ok")) else 1
    if command in {"status", "inspect", "next-action", "reconcile", "repair-plan", "validate", "attest", "export", "queue-list"}:
        return 0
    if command in {"retry-node", "repair-apply", "cancel", "adopt", "queue-add", "queue-run", "queue-retry", "queue-cancel", "queue-remove", "queue-export", "queue-import"}:
        return 0 if payload.get("status") in {"available", "complete", "succeeded", "already_adopted", "planned", "requested", "added", "completed", "removed", "exported", "imported"} else 1
    if command in {"run", "resume"}:
        return 0 if payload.get("job_status") == "completed" and payload.get("completion_status") == "complete" else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root or getattr(args, "doctor_repo_root", "")
    control = ReviewControlPlane(repo_root=repo_root or None)
    try:
        if args.command.startswith("queue-"):
            payload = _queue_command(args)
        elif args.command == "doctor":
            payload = control.doctor(
                config_path=(getattr(args, "doctor_config", "") or args.config or None),
                workspace=args.workspace or None,
            )
        elif args.command == "plan":
            payload = control.plan(args.spec)
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
        elif args.command == "repair-plan":
            payload = control.repair_plan(job_id=args.job or None, workspace=args.workspace or None)
        elif args.command == "repair-apply":
            payload = control.repair_apply(
                job_id=args.job or None,
                workspace=args.workspace or None,
                plan_id=args.plan,
            )
        elif args.command == "validate":
            payload = control.validate(job_id=args.job or None, workspace=args.workspace or None)
        elif args.command == "cancel":
            payload = control.cancel(
                job_id=args.job or None,
                workspace=args.workspace or None,
                reason=args.reason,
            )
        elif args.command == "adopt":
            payload = control.adopt(
                job_id=args.job or None,
                workspace=args.workspace or None,
                artifact_id=args.artifact,
                adopted_by=args.actor,
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
    except (ControlPlaneError, OSError, ValueError, TypeError) as exc:
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
