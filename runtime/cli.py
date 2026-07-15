from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import importlib
from typing import Any

from runtime.job_spec import load_runtime_job_spec
from runtime.runner import AgentRuntimeRunner
from services.console_io import configure_utf8_stdio, write_ascii_json_line


def _load_symbol(reference: str) -> Any:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("symbol reference must use module:attribute")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-generate-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "resume"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("spec")
        subparser.add_argument("--job-id", default="")
        subparser.add_argument("--stage-handler", default="")
        subparser.add_argument("--validator-module", default="")
    for command in ("status", "reconcile"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("workspace")
    migrate_legacy = subparsers.add_parser("migrate-legacy")
    migrate_legacy.add_argument("workspace")
    migrate_legacy.add_argument("--actor", required=True)
    migrate_legacy.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "status":
        write_ascii_json_line(asdict(AgentRuntimeRunner.status(args.workspace)))
        return 0
    if args.command == "reconcile":
        write_ascii_json_line(asdict(AgentRuntimeRunner.reconcile(args.workspace)))
        return 0
    if args.command == "migrate-legacy":
        write_ascii_json_line(
            asdict(
                AgentRuntimeRunner.migrate_legacy(
                    args.workspace,
                    args.actor,
                    args.reason,
                )
            )
        )
        return 0

    spec = load_runtime_job_spec(args.spec)
    if args.job_id:
        spec = replace(spec, job_id=args.job_id)
    stage_handler = _load_symbol(args.stage_handler) if args.stage_handler else None
    validator_module = importlib.import_module(args.validator_module) if args.validator_module else None
    import main as legacy_main

    runner = AgentRuntimeRunner(
        spec,
        legacy_main=legacy_main,
        stage_handler=stage_handler,
        validator_module=validator_module,
    )
    result = runner.resume() if args.command == "resume" else runner.run()
    write_ascii_json_line(asdict(result))
    return 0 if result.job_status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
