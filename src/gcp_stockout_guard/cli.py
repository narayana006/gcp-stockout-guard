"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from . import __version__
from .advice import Advice, AdviceFormatError, build_capacity_request, load_advice
from .plan import (
    advice_payload,
    build_plan,
    render_advice_text,
    render_plan_json,
    render_plan_text,
)
from .stockouts import (
    LogFormatError,
    find_stockouts,
    load_entries,
    render_json,
    render_text,
    summarize,
)

EXIT_OK = 0
EXIT_STOCKOUTS = 1
EXIT_USAGE = 2


class InputError(Exception):
    """A file could not be read or understood; the message is shown to the user."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcp-stockout-guard",
        description=(
            "Spot Google Cloud capacity stockouts from Compute Engine error logs and saved "
            "capacity advice. Reads files only; never calls Google Cloud."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    stockouts = commands.add_parser(
        "stockouts",
        help="count stockout errors per zone and machine type in exported log entries",
        description=(
            "Read log entries exported with `gcloud logging read --format=json` and count "
            "ZONE_RESOURCE_POOL_EXHAUSTED errors per zone and machine type."
        ),
    )
    stockouts.add_argument(
        "file",
        nargs="?",
        default="-",
        help="JSON file of log entries, or - for standard input (default: -)",
    )
    stockouts.add_argument(
        "--format", choices=("text", "json"), default="text", help="output format"
    )
    stockouts.add_argument(
        "--fail-on-stockout",
        action="store_true",
        help="exit with status 1 when any stockout error is found",
    )

    advice = commands.add_parser(
        "advice",
        help="summarise saved advice.capacity and advice.calendarMode responses",
        description=(
            "Read saved JSON responses from Compute Engine's advice.capacity (beta, Spot VMs "
            "only) or advice.calendarMode methods and list what they recommend."
        ),
    )
    advice.add_argument("files", nargs="+", metavar="FILE", help="saved response (JSON)")
    advice.add_argument("--format", choices=("text", "json"), default="text", help="output format")

    plan = commands.add_parser(
        "plan",
        help="rank fallback zones for stocked-out machine types and print a fallback plan",
        description=(
            "Combine stockout errors from exported logs with optional saved advice responses, "
            "rank fallback zones per machine type and print a plan. Any reservation step is "
            "printed as a command for you to review; nothing is executed."
        ),
    )
    plan.add_argument(
        "--logs", required=True, metavar="FILE", help="exported log entries, or - for stdin"
    )
    plan.add_argument(
        "--advice",
        action="append",
        default=[],
        metavar="FILE",
        help="saved advice response; repeat for several files",
    )
    plan.add_argument("--machine-type", help="only plan for this machine type")
    plan.add_argument(
        "--count",
        type=int,
        metavar="N",
        help="VMs you need; adds a dry-run reservation command for the best fallback zone",
    )
    plan.add_argument("--project", help="project ID to put in the printed reservation command")
    plan.add_argument("--format", choices=("text", "json"), default="text", help="output format")

    request = commands.add_parser(
        "capacity-request",
        help="print the JSON body for an advice.capacity call (prints only, sends nothing)",
        description=(
            "Print the request body for the advice.capacity method (beta, Spot VMs only) so "
            "you can send it yourself and save the response for the `advice` and `plan` commands."
        ),
    )
    request.add_argument(
        "--machine-type", action="append", required=True, help="machine type; repeat for several"
    )
    request.add_argument("--count", type=int, required=True, metavar="N", help="number of VMs")
    request.add_argument("--zone", action="append", default=[], help="limit to this zone")
    request.add_argument(
        "--target-shape", choices=("ANY", "ANY_SINGLE_ZONE", "BALANCED"), help="distribution shape"
    )
    return parser


def _read(path: str, stdin: TextIO) -> str:
    if path == "-":
        return stdin.read()
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or exc}") from exc


def _load_logs(path: str, stdin: TextIO):
    try:
        return load_entries(_read(path, stdin))
    except LogFormatError as exc:
        raise InputError(str(exc)) from exc


def _load_advice(paths: Sequence[str], stdin: TextIO) -> Advice:
    merged = Advice()
    for path in paths:
        try:
            parsed = load_advice(_read(path, stdin))
        except AdviceFormatError as exc:
            raise InputError(f"{path}: {exc}") from exc
        merged.spot.extend(parsed.spot)
        merged.calendar.extend(parsed.calendar)
    return merged


def _run(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    if args.command == "stockouts":
        entries = _load_logs(args.file, stdin)
        groups = summarize(find_stockouts(entries))
        render = render_json if args.format == "json" else render_text
        print(render(groups, len(entries)), file=stdout)
        return EXIT_STOCKOUTS if groups and args.fail_on_stockout else EXIT_OK

    if args.command == "advice":
        advice = _load_advice(args.files, stdin)
        if args.format == "json":
            print(json.dumps(advice_payload(advice), indent=2), file=stdout)
        else:
            print(render_advice_text(advice), file=stdout)
        return EXIT_OK

    if args.command == "plan":
        if args.count is not None and args.count < 1:
            raise InputError("--count must be at least 1")
        entries = _load_logs(args.logs, stdin)
        advice = _load_advice(args.advice, stdin)
        groups = summarize(find_stockouts(entries))
        plan = build_plan(groups, advice, args.machine_type, args.count, args.project)
        render = render_plan_json if args.format == "json" else render_plan_text
        print(render(plan, len(entries)), file=stdout)
        return EXIT_OK

    try:
        body = build_capacity_request(
            args.machine_type, args.count, args.zone, args.target_shape
        )
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    print(json.dumps(body, indent=2), file=stdout)
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return _run(args, stdin, stdout)
    except InputError as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_USAGE
