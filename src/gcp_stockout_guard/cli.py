"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from . import __version__
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcp-stockout-guard",
        description="Spot Google Cloud capacity stockouts from Compute Engine error logs.",
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
    return parser


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
        if args.file == "-":
            text = stdin.read()
        else:
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read()
        entries = load_entries(text)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc.strerror or exc}", file=stderr)
        return EXIT_USAGE
    except LogFormatError as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_USAGE

    groups = summarize(find_stockouts(entries))
    render = render_json if args.format == "json" else render_text
    print(render(groups, len(entries)), file=stdout)
    if groups and args.fail_on_stockout:
        return EXIT_STOCKOUTS
    return EXIT_OK
