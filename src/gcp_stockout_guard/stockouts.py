"""Find Compute Engine stockout errors in exported Cloud Logging entries.

The input is the JSON that ``gcloud logging read --format=json`` prints: a list
of LogEntry objects. Nothing here talks to Google Cloud; it only reads entries
that were already exported.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

UNKNOWN = "unknown"

# Substrings that mark a log entry as a capacity stockout. The first also
# matches ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS.
STOCKOUT_MARKERS = (
    "ZONE_RESOURCE_POOL_EXHAUSTED",
    "does not have enough resources available to fulfill the request",
)

_ZONE_IN_PATH = re.compile(r"zones/([a-z]+-[a-z]+[0-9]+-[a-z])\b")
_MACHINE_TYPE_IN_PATH = re.compile(r"machineTypes/([a-z0-9]+(?:-[a-z0-9]+)+)\b")
_MACHINE_TYPE_IN_MESSAGE = re.compile(
    r"\bA ([a-z0-9]+(?:-[a-z0-9]+)+) VM instance is currently unavailable"
)
_ZONE_IN_MESSAGE = re.compile(r"unavailable in the ([a-z]+-[a-z]+[0-9]+-[a-z]) zone")
_FRACTION = re.compile(r"\.(\d+)")


class LogFormatError(ValueError):
    """The input is not a list of log entries."""


@dataclass(frozen=True)
class StockoutEvent:
    zone: str
    machine_type: str
    timestamp: datetime | None
    suggested_zones: tuple[str, ...] = ()


@dataclass
class StockoutGroup:
    zone: str
    machine_type: str
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    suggested: Counter = field(default_factory=Counter)

    @property
    def suggested_zones(self) -> list[str]:
        """Zones Google suggested, most often suggested first."""
        return [z for z, _ in sorted(self.suggested.items(), key=lambda kv: (-kv[1], kv[0]))]


def load_entries(text: str) -> list[dict[str, Any]]:
    """Parse a JSON array of log entries, or one JSON entry per line."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LogFormatError(f"line {number} is not valid JSON: {exc.msg}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        raise LogFormatError("expected a JSON list of log entries")
    return data


def _walk(node: Any) -> Iterator[Any]:
    """Yield every dict and string nested anywhere inside a log entry."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)
    elif isinstance(node, str):
        yield node


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an RFC 3339 timestamp, including nanosecond precision and a Z suffix."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # datetime keeps microseconds only; Cloud Logging writes up to nanoseconds.
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_match(pattern: re.Pattern[str], strings: Iterable[str]) -> str | None:
    for text in strings:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _split_zones(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = [p for p in value if isinstance(p, str)]
    else:
        return []
    return [p.strip() for p in parts if p.strip()]


def parse_entry(entry: dict[str, Any]) -> StockoutEvent | None:
    """Return the stockout described by one log entry, or None if it is not one."""
    nodes = list(_walk(entry))
    strings = [n for n in nodes if isinstance(n, str)]
    if not any(marker in text for text in strings for marker in STOCKOUT_MARKERS):
        return None

    # Compute Engine attaches structured details to the error as an ErrorInfo
    # "metadatas" map: zone, vmType and zonesAvailable (comma-separated).
    zone = machine_type = None
    suggested: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if "zonesAvailable" in node or "vmType" in node:
            if isinstance(node.get("zone"), str):
                zone = zone or node["zone"]
            if isinstance(node.get("vmType"), str):
                machine_type = machine_type or node["vmType"]
            for candidate in _split_zones(node.get("zonesAvailable")):
                if candidate not in suggested:
                    suggested.append(candidate)

    if not zone:
        resource = entry.get("resource")
        labels = resource.get("labels") if isinstance(resource, dict) else None
        label_zone = labels.get("zone") if isinstance(labels, dict) else None
        zone = (
            (label_zone if isinstance(label_zone, str) and label_zone else None)
            or _first_match(_ZONE_IN_PATH, strings)
            or _first_match(_ZONE_IN_MESSAGE, strings)
        )
    if not machine_type:
        machine_type = _first_match(_MACHINE_TYPE_IN_PATH, strings) or _first_match(
            _MACHINE_TYPE_IN_MESSAGE, strings
        )

    zone = zone or UNKNOWN
    return StockoutEvent(
        zone=zone,
        machine_type=machine_type or UNKNOWN,
        timestamp=parse_timestamp(entry.get("timestamp")),
        suggested_zones=tuple(z for z in suggested if z != zone),
    )


def find_stockouts(entries: Iterable[dict[str, Any]]) -> list[StockoutEvent]:
    events = []
    for entry in entries:
        event = parse_entry(entry)
        if event is not None:
            events.append(event)
    return events


def summarize(events: Iterable[StockoutEvent]) -> list[StockoutGroup]:
    """Count stockouts per zone and machine type, busiest pair first."""
    groups: dict[tuple[str, str], StockoutGroup] = {}
    for event in events:
        key = (event.zone, event.machine_type)
        group = groups.setdefault(
            key, StockoutGroup(zone=event.zone, machine_type=event.machine_type)
        )
        group.count += 1
        group.suggested.update(event.suggested_zones)
        if event.timestamp is not None:
            if group.first_seen is None or event.timestamp < group.first_seen:
                group.first_seen = event.timestamp
            if group.last_seen is None or event.timestamp > group.last_seen:
                group.last_seen = event.timestamp
    return sorted(groups.values(), key=lambda g: (-g.count, g.zone, g.machine_type))


def _format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def render_text(groups: list[StockoutGroup], entries_read: int) -> str:
    if not groups:
        return f"No stockout errors found in {entries_read} log entries."
    total = sum(g.count for g in groups)
    header = (
        "ZONE",
        "MACHINE TYPE",
        "ERRORS",
        "FIRST SEEN (UTC)",
        "LAST SEEN (UTC)",
        "GOOGLE SUGGESTS",
    )
    rows = [
        (
            g.zone,
            g.machine_type,
            str(g.count),
            _format_time(g.first_seen),
            _format_time(g.last_seen),
            ", ".join(g.suggested_zones) or "-",
        )
        for g in groups
    ]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = [
        f"{total} stockout errors across {len(groups)} zone and machine type pairs "
        f"({entries_read} log entries read)",
        "",
    ]
    for row in [header, *rows]:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def render_json(groups: list[StockoutGroup], entries_read: int) -> str:
    payload = {
        "entries_read": entries_read,
        "stockout_errors": sum(g.count for g in groups),
        "groups": [
            {
                "zone": g.zone,
                "machine_type": g.machine_type,
                "errors": g.count,
                "first_seen": g.first_seen.isoformat() if g.first_seen else None,
                "last_seen": g.last_seen.isoformat() if g.last_seen else None,
                "suggested_zones": g.suggested_zones,
            }
            for g in groups
        ],
    }
    return json.dumps(payload, indent=2)
