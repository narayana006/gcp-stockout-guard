"""Rank fallback zones and build a fallback plan from logs and saved advice.

Everything here is read-only analysis. The reservation command in a plan is a
string for a person to review and run; this package never executes it.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field

from .advice import Advice, CalendarAdvice, SpotAdvice, format_uptime, format_window
from .stockouts import UNKNOWN, StockoutGroup, _format_time, parse_timestamp

# A zone and machine type pair is HIGH risk from this many stockout errors, MEDIUM from
# MEDIUM_ERRORS, otherwise LOW.
HIGH_ERRORS = 5
MEDIUM_ERRORS = 2


@dataclass
class ZoneOption:
    """A candidate zone for one machine type, with the evidence behind its rank."""

    zone: str
    suggested: int = 0  # times Google named it as an alternative in a stockout error
    stockouts: int = 0  # stockout errors seen in this zone for the same machine type
    spot_obtainability: float | None = None
    spot_uptime_seconds: float | None = None
    score: float = 0.0

    @property
    def evidence(self) -> str:
        parts = []
        if self.suggested:
            parts.append(f"suggested by Google in {self.suggested} error(s)")
        if self.spot_obtainability is not None:
            parts.append(f"Spot obtainability {self.spot_obtainability:.2f}")
        if self.stockouts:
            parts.append(f"{self.stockouts} stockout error(s) of its own")
        return "; ".join(parts) or "no evidence"


@dataclass
class PlanItem:
    """What to do about one zone and machine type that ran out of capacity."""

    zone: str
    machine_type: str
    errors: int
    risk: str
    last_seen: str | None
    fallbacks: list[ZoneOption] = field(default_factory=list)
    reservation_command: str | None = None


@dataclass
class Plan:
    items: list[PlanItem]
    spot: list[SpotAdvice]
    calendar: list[CalendarAdvice]
    count: int | None = None


def risk_level(errors: int) -> str:
    if errors >= HIGH_ERRORS:
        return "HIGH"
    if errors >= MEDIUM_ERRORS:
        return "MEDIUM"
    return "LOW"


def rank_zones(
    machine_type: str, groups: list[StockoutGroup], spot: list[SpotAdvice]
) -> list[ZoneOption]:
    """Rank every zone there is evidence about for ``machine_type``, best first.

    score = share of this machine type's stockout errors that suggested the zone
          + Spot obtainability (0 when there is no advice for the zone)
          - share of this machine type's stockout errors that happened in the zone

    Ties are broken by zone name so the order is stable.
    """
    own = [g for g in groups if g.machine_type == machine_type]
    total_errors = sum(g.count for g in own) or 1
    options: dict[str, ZoneOption] = {}

    def option(zone: str) -> ZoneOption:
        return options.setdefault(zone, ZoneOption(zone=zone))

    for group in own:
        if group.zone != UNKNOWN:
            option(group.zone).stockouts += group.count
        for zone, times in group.suggested.items():
            option(zone).suggested += times
    for item in spot:
        if item.machine_type != machine_type:
            continue
        current = option(item.zone)
        # Several saved responses may mention the same zone; keep the most cautious score.
        if item.obtainability is not None and (
            current.spot_obtainability is None or item.obtainability < current.spot_obtainability
        ):
            current.spot_obtainability = item.obtainability
            current.spot_uptime_seconds = item.estimated_uptime_seconds

    for current in options.values():
        current.score = round(
            current.suggested / total_errors
            + (current.spot_obtainability or 0.0)
            - current.stockouts / total_errors,
            4,
        )
    return sorted(options.values(), key=lambda o: (-o.score, o.zone))


def reservation_command(
    zone: str, machine_type: str, count: int, project: str | None = None
) -> str:
    """The gcloud command a person could run to reserve on-demand capacity."""
    name = f"stockout-guard-{machine_type}-{zone}"
    parts = ["gcloud", "compute", "reservations", "create", name]
    parts += [f"--zone={zone}", f"--machine-type={machine_type}", f"--vm-count={count}"]
    if project:
        parts.append(f"--project={project}")
    return " ".join(shlex.quote(p) for p in parts)


def build_plan(
    groups: list[StockoutGroup],
    advice: Advice | None = None,
    machine_type: str | None = None,
    count: int | None = None,
    project: str | None = None,
) -> Plan:
    """One plan item per stocked-out zone and machine type, most errors first.

    Fallbacks are the ranked zones other than the stocked-out one, leaving out
    zones whose score is not positive. A reservation command is added for the
    best fallback when ``count`` is given.
    """
    advice = advice or Advice()
    items = []
    for group in groups:
        if machine_type and group.machine_type != machine_type:
            continue
        ranked = rank_zones(group.machine_type, groups, advice.spot)
        fallbacks = [o for o in ranked if o.zone != group.zone and o.score > 0]
        item = PlanItem(
            zone=group.zone,
            machine_type=group.machine_type,
            errors=group.count,
            risk=risk_level(group.count),
            last_seen=group.last_seen.isoformat() if group.last_seen else None,
            fallbacks=fallbacks,
        )
        if count and fallbacks and group.machine_type != UNKNOWN:
            item.reservation_command = reservation_command(
                fallbacks[0].zone, group.machine_type, count, project
            )
        items.append(item)
    spot = [s for s in advice.spot if not machine_type or s.machine_type == machine_type]
    return Plan(items=items, spot=spot, calendar=list(advice.calendar), count=count)


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]], indent: str = "") -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    return [
        indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in [header, *rows]
    ]


def render_spot(spot: list[SpotAdvice]) -> list[str]:
    rows = [
        (
            s.zone,
            s.machine_type,
            str(s.instance_count) if s.instance_count is not None else "-",
            f"{s.obtainability:.2f}" if s.obtainability is not None else "-",
            format_uptime(s.estimated_uptime_seconds),
        )
        for s in sorted(spot, key=lambda s: (-(s.obtainability or 0.0), s.zone, s.machine_type))
    ]
    header = ("ZONE", "MACHINE TYPE", "VMS", "OBTAINABILITY", "EST. UPTIME")
    return ["Spot capacity advice (advice.capacity, beta, Spot VMs only)", *_table(header, rows)]


def render_calendar(calendar: list[CalendarAdvice]) -> list[str]:
    lines = ["Calendar-mode advice (advice.calendarMode, future reservations)"]
    for item in calendar:
        if item.zone:
            window = format_window(item.start_time, item.end_time)
            lines.append(f"  {item.spec}: recommended {item.zone}, {window}")
        else:
            lines.append(f"  {item.spec}: no zone recommended")
        for zone, (status, details) in sorted(item.other_zones.items()):
            note = f" - {details}" if details else ""
            lines.append(f"    not recommended: {zone} ({status or 'no status'}){note}")
    return lines


def render_advice_text(advice: Advice) -> str:
    sections = []
    if advice.spot:
        sections.append("\n".join(render_spot(advice.spot)))
    if advice.calendar:
        sections.append("\n".join(render_calendar(advice.calendar)))
    return "\n\n".join(sections) or "No recommendations found in the advice files."


def render_plan_text(plan: Plan, entries_read: int) -> str:
    if not plan.items:
        lines = [f"No stockout errors to plan around in {entries_read} log entries."]
    else:
        lines = [
            f"Fallback plan for {len(plan.items)} zone and machine type pair(s) "
            f"that ran out of capacity ({entries_read} log entries read)"
        ]
    for number, item in enumerate(plan.items, start=1):
        seen = _format_time(parse_timestamp(item.last_seen))
        lines += [
            "",
            f"{number}. {item.machine_type} in {item.zone}: {item.errors} stockout error(s), "
            f"risk {item.risk}, last seen {seen} UTC",
        ]
        if not item.fallbacks:
            lines.append(
                "   No fallback zone has supporting evidence. Try another machine type or region."
            )
            continue
        lines.append("   Fallback zones, best first:")
        rows = [
            (str(rank), o.zone, f"{o.score:.2f}", o.evidence)
            for rank, o in enumerate(item.fallbacks, start=1)
        ]
        lines += _table(("#", "ZONE", "SCORE", "EVIDENCE"), rows, indent="   ")
        if item.reservation_command:
            lines += [
                f"   DRY RUN, not executed. To reserve {plan.count} on-demand VM(s) "
                f"in {item.fallbacks[0].zone}, review and run:",
                f"     {item.reservation_command}",
            ]
    if plan.spot:
        lines += ["", *render_spot(plan.spot)]
    if plan.calendar:
        lines += ["", *render_calendar(plan.calendar)]
    return "\n".join(lines)


def advice_payload(advice: Advice) -> dict:
    return {
        "spot": [
            {
                "zone": s.zone,
                "machine_type": s.machine_type,
                "instance_count": s.instance_count,
                "obtainability": s.obtainability,
                "estimated_uptime_seconds": s.estimated_uptime_seconds,
                "provisioning_model": s.provisioning_model,
            }
            for s in advice.spot
        ],
        "calendar": [
            {
                "spec": c.spec,
                "recommended_zone": c.zone,
                "start_time": c.start_time,
                "end_time": c.end_time,
                "recommendation_type": c.recommendation_type,
                "other_zones": {
                    zone: {"status": status, "details": details}
                    for zone, (status, details) in sorted(c.other_zones.items())
                },
            }
            for c in advice.calendar
        ],
    }


def render_plan_json(plan: Plan, entries_read: int) -> str:
    payload = {
        "entries_read": entries_read,
        "dry_run": True,
        "items": [
            {
                "zone": item.zone,
                "machine_type": item.machine_type,
                "errors": item.errors,
                "risk": item.risk,
                "last_seen": item.last_seen,
                "fallbacks": [
                    {
                        "zone": o.zone,
                        "score": o.score,
                        "suggested": o.suggested,
                        "stockouts": o.stockouts,
                        "spot_obtainability": o.spot_obtainability,
                    }
                    for o in item.fallbacks
                ],
                "reservation_command": item.reservation_command,
            }
            for item in plan.items
        ],
        **advice_payload(Advice(spot=plan.spot, calendar=plan.calendar)),
    }
    return json.dumps(payload, indent=2)
