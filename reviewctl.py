"""Machine-readable Agent control plane entry point.

Use ``python -m reviewctl ...`` from a checkout, or import ``main`` from
tests/host integrations.  All commands emit one JSON object and never print
provider credentials.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from runtime.control_plane import ControlPlaneError, ReviewControlPlane
from services.console_io import configure_utf8_stdio, write_ascii_json_line


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
    return parser


def _exit_code(command: str, payload: dict[str, Any]) -> int:
    if command == "doctor":
        return 0 if bool(payload.get("ok")) else 1
    if command in {"status", "inspect", "next-action", "reconcile", "repair-plan", "validate", "attest", "export"}:
        return 0
    if command in {"retry-node", "repair-apply", "cancel", "adopt"}:
        return 0 if payload.get("status") in {"available", "complete", "succeeded", "already_adopted", "planned", "requested"} else 1
    if command in {"run", "resume"}:
        return 0 if payload.get("job_status") == "completed" and payload.get("completion_status") == "complete" else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root or getattr(args, "doctor_repo_root", "")
    control = ReviewControlPlane(repo_root=repo_root or None)
    try:
        if args.command == "doctor":
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
