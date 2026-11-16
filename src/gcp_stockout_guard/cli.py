"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from . import __version__
from .advice import (
    Advice,
    AdviceFormatError,
    build_calendar_request,
    build_capacity_request,
    load_advice,
    parse_duration_arg,
    parse_time_arg,
)
from .live import LiveClient, LiveError, region_of
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
            "capacity advice. Analysis commands read files only; the fetch-* commands make "
            "read-only calls to Google Cloud. Nothing creates, changes or buys any resource."
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

    fetch_logs = commands.add_parser(
        "fetch-logs",
        help="read stockout errors from Cloud Logging (read-only) and print them as JSON",
        description=(
            "Call Cloud Logging entries:list for stockout errors in one project and print the "
            "entries as a JSON list, ready for `stockouts -` or `plan --logs -`."
        ),
    )
    fetch_logs.add_argument("--project", required=True, help="project ID to read logs from")
    fetch_logs.add_argument(
        "--days", type=float, default=7, help="how far back to look (default: 7)"
    )
    fetch_logs.add_argument(
        "--max-entries", type=int, default=5000, metavar="N", help="stop after N entries"
    )

    fetch_capacity = commands.add_parser(
        "fetch-capacity",
        help="call advice.capacity (beta, Spot VMs only, read-only) and print the response",
        description=(
            "Send an advice.capacity request and print the JSON response, ready to save for "
            "the `advice` and `plan` commands."
        ),
    )
    fetch_capacity.add_argument("--project", required=True, help="project ID")
    fetch_capacity.add_argument("--region", help="region; derived from --zone when omitted")
    fetch_capacity.add_argument(
        "--machine-type", action="append", required=True, help="machine type; repeat for several"
    )
    fetch_capacity.add_argument(
        "--count", type=int, required=True, metavar="N", help="number of VMs"
    )
    fetch_capacity.add_argument("--zone", action="append", default=[], help="limit to this zone")
    fetch_capacity.add_argument(
        "--target-shape", choices=("ANY", "ANY_SINGLE_ZONE", "BALANCED"), help="distribution shape"
    )

    fetch_calendar = commands.add_parser(
        "fetch-calendar",
        help="call advice.calendarMode (read-only) and print the response",
        description=(
            "Ask advice.calendarMode where and when a future reservation for one machine "
            "type is likely to be approved, and print the JSON response, ready for the "
            "`advice` and `plan` commands. This returns advice only; it reserves nothing."
        ),
    )
    fetch_calendar.add_argument("--project", required=True, help="project ID")
    fetch_calendar.add_argument("--region", help="region; derived from --zone when omitted")
    fetch_calendar.add_argument(
        "--machine-type", required=True, help="machine type, e.g. a3-highgpu-8g"
    )
    fetch_calendar.add_argument(
        "--count", type=int, required=True, metavar="N", help="number of VMs"
    )
    fetch_calendar.add_argument(
        "--start", required=True, metavar="TIME",
        help="earliest start, e.g. 2026-12-01 or 2026-12-01T09:00 (UTC unless an offset is given)",
    )
    fetch_calendar.add_argument(
        "--latest-start", metavar="TIME", help="latest acceptable start (default: --start)"
    )
    fetch_calendar.add_argument(
        "--duration", required=True, help="minimum run time, e.g. 7d, 36h or 90m"
    )
    fetch_calendar.add_argument(
        "--max-duration", help="maximum run time (default: --duration)"
    )
    fetch_calendar.add_argument(
        "--zone", action="append", default=[], help="allow this zone; repeat for several"
    )
    fetch_calendar.add_argument(
        "--exclude-zone", action="append", default=[], help="never recommend this zone"
    )
    fetch_calendar.add_argument(
        "--spec", default="request-1", help="name for this request in the response"
    )
    return parser


def _calendar_body(args: argparse.Namespace) -> dict:
    try:
        return build_calendar_request(
            args.machine_type,
            args.count,
            parse_time_arg(args.start),
            parse_duration_arg(args.duration),
            parse_time_arg(args.latest_start) if args.latest_start else None,
            parse_duration_arg(args.max_duration) if args.max_duration else None,
            args.zone,
            args.exclude_zone,
            args.spec,
        )
    except ValueError as exc:
        raise InputError(str(exc)) from exc


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


def _run(
    args: argparse.Namespace, stdin: TextIO, stdout: TextIO, client: LiveClient | None
) -> int:
    if args.command == "fetch-logs":
        try:
            entries = (client or LiveClient()).fetch_stockout_logs(
                args.project, args.days, args.max_entries
            )
        except ValueError as exc:
            raise InputError(str(exc)) from exc
        print(json.dumps(entries, indent=2), file=stdout)
        return EXIT_OK

    if args.command == "fetch-capacity":
        region = args.region or region_of(args.zone)
        if not region:
            raise InputError("give --region, or --zone values that are all in one region")
        try:
            body = build_capacity_request(
                args.machine_type, args.count, args.zone, args.target_shape
            )
        except ValueError as exc:
            raise InputError(str(exc)) from exc
        response = (client or LiveClient()).fetch_capacity_advice(args.project, region, body)
        print(json.dumps(response, indent=2), file=stdout)
        return EXIT_OK

    if args.command == "fetch-calendar":
        body = _calendar_body(args)
        region = args.region or region_of(args.zone + args.exclude_zone)
        if not region:
            raise InputError("give --region, or --zone values that are all in one region")
        response = (client or LiveClient()).fetch_calendar_advice(args.project, region, body)
        print(json.dumps(response, indent=2), file=stdout)
        return EXIT_OK

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
    client: LiveClient | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return _run(args, stdin, stdout, client)
    except (InputError, LiveError) as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_USAGE
