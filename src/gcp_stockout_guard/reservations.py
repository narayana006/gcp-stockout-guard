"""Compare what you run, and what autoscalers may scale to, with existing reservations.

Reads saved ``gcloud ... list --format=json`` output only. Nothing here calls Google
Cloud or creates a reservation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .advice import short_zone
from .plan import _table

# Instance states that hold (or are about to hold) capacity in a zone.
ACTIVE_STATES = {"PROVISIONING", "STAGING", "RUNNING", "REPAIRING"}


class InventoryFormatError(ValueError):
    """Raised when a saved list output is not a JSON list of resources."""


@dataclass
class Gap:
    """Demand against reservations for one zone and machine type."""

    zone: str
    machine_type: str
    running: int = 0  # active on-demand VMs, inside or outside managed groups
    standalone: int = 0  # active on-demand VMs not in a managed group from --groups
    group_max: int = 0  # per-zone maximum of the managed groups in this zone
    reserved: int = 0  # VMs covered by READY reservations
    in_use: int = 0  # of which reservations report as consumed

    @property
    def needed(self) -> int:
        return max(self.running, self.standalone + self.group_max)

    @property
    def shortfall(self) -> int:
        return max(0, self.needed - self.reserved)

    @property
    def spare(self) -> int:
        return max(0, self.reserved - self.needed)


def load_list(text: str, what: str) -> list[dict[str, Any]]:
    """Parse ``gcloud ... list --format=json`` output (a JSON list of objects)."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InventoryFormatError(f"{what}: not valid JSON ({exc.msg})") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise InventoryFormatError(f"{what}: expected a JSON list, as printed by --format=json")
    return data


def _location_key(url: Any) -> str | None:
    """``.../zones/Z/instanceGroupManagers/NAME`` -> ``zones/Z/NAME`` (project-agnostic)."""
    if not isinstance(url, str):
        return None
    parts = url.rstrip("/").split("/")
    for kind in ("zones", "regions"):
        if kind in parts:
            i = parts.index(kind)
            if i + 1 < len(parts):
                return f"{kind}/{parts[i + 1]}/{parts[-1]}"
    return None


def _is_spot(resource: dict[str, Any]) -> bool:
    scheduling = resource.get("scheduling") or {}
    return scheduling.get("provisioningModel") == "SPOT" or bool(scheduling.get("preemptible"))


def _created_by(instance: dict[str, Any]) -> str | None:
    for item in (instance.get("metadata") or {}).get("items") or []:
        if isinstance(item, dict) and item.get("key") == "created-by":
            return _location_key(item.get("value"))
    return None


def _group_max(group: dict[str, Any], autoscalers: dict[str, dict[str, Any]]) -> int:
    autoscaler = group.get("autoscaler") or autoscalers.get(_location_key(group.get("selfLink")))
    policy = (autoscaler or {}).get("autoscalingPolicy") or {}
    if autoscaler and policy.get("mode", "ON") != "OFF" and "maxNumReplicas" in policy:
        return int(policy["maxNumReplicas"])
    return int(group.get("targetSize") or 0)


def _group_zones(group: dict[str, Any]) -> list[str]:
    if group.get("zone"):
        return [short_zone(group["zone"])]
    zones = ((group.get("distributionPolicy") or {}).get("zones")) or []
    return [short_zone(z.get("zone")) for z in zones if isinstance(z, dict) and z.get("zone")]


def find_gaps(
    instances: list[dict[str, Any]],
    reservations: list[dict[str, Any]],
    groups: list[dict[str, Any]] | None = None,
    templates: list[dict[str, Any]] | None = None,
    autoscalers: list[dict[str, Any]] | None = None,
) -> tuple[list[Gap], list[str]]:
    """Return one :class:`Gap` per zone and machine type, biggest shortfall first,
    plus notes about inputs that could not be used."""
    gaps: dict[tuple[str, str], Gap] = {}
    notes: list[str] = []

    def gap(zone: str, machine_type: str) -> Gap:
        return gaps.setdefault((zone, machine_type), Gap(zone, machine_type))

    template_props = {t.get("name"): t.get("properties") or {} for t in templates or []}
    scalers = {_location_key(a.get("target")): a for a in autoscalers or []}
    grouped: set[str] = set()
    for group in groups or []:
        key = _location_key(group.get("selfLink"))
        if key:
            grouped.add(key)
        template = short_zone(group.get("instanceTemplate"))
        props = template_props.get(template) or {}
        machine_type = short_zone(props.get("machineType"))
        name = group.get("name", "?")
        if not machine_type:
            notes.append(f"group {name}: template {template} not found in --templates")
            continue
        if _is_spot(props):
            continue  # Spot VMs cannot use reservations
        zones = _group_zones(group)
        if not zones:
            notes.append(f"group {name}: no zone found")
            continue
        per_zone = math.ceil(_group_max(group, scalers) / len(zones))
        for zone in zones:
            gap(zone, machine_type).group_max += per_zone

    for instance in instances:
        if instance.get("status") not in ACTIVE_STATES or _is_spot(instance):
            continue
        g = gap(short_zone(instance.get("zone")) or "unknown",
                short_zone(instance.get("machineType")) or "unknown")
        g.running += 1
        if _created_by(instance) not in grouped:
            g.standalone += 1

    for reservation in reservations:
        if reservation.get("status", "READY") != "READY":
            continue
        specific = reservation.get("specificReservation") or {}
        props = specific.get("instanceProperties") or {}
        g = gap(short_zone(reservation.get("zone")) or "unknown",
                short_zone(props.get("machineType")) or "unknown")
        g.reserved += int(specific.get("count") or 0)
        g.in_use += int(specific.get("inUseCount") or 0)

    ordered = sorted(gaps.values(), key=lambda g: (-g.shortfall, g.zone, g.machine_type))
    return ordered, notes


def render_gaps_text(gaps: list[Gap], notes: list[str]) -> str:
    if not gaps:
        return "No on-demand VMs, managed groups or reservations found."
    short = [g for g in gaps if g.shortfall]
    header = ("ZONE", "MACHINE TYPE", "RUNNING", "GROUP MAX", "NEEDED", "RESERVED", "IN USE", "GAP")
    rows = [
        (
            g.zone, g.machine_type, str(g.running), str(g.group_max), str(g.needed),
            str(g.reserved), str(g.in_use),
            f"short {g.shortfall}" if g.shortfall else (f"spare {g.spare}" if g.spare else "ok"),
        )
        for g in gaps
    ]
    lines = [
        f"{len(short)} of {len(gaps)} zone and machine type pair(s) need more reserved VMs "
        f"({sum(g.shortfall for g in short)} VM(s) in total)",
        "",
        *_table(header, rows),
    ]
    if notes:
        lines += ["", "Not counted:", *(f"  {note}" for note in notes)]
    return "\n".join(lines)


def render_gaps_json(gaps: list[Gap], notes: list[str]) -> str:
    payload = {
        "total_shortfall": sum(g.shortfall for g in gaps),
        "gaps": [
            {
                "zone": g.zone, "machine_type": g.machine_type, "running": g.running,
                "standalone": g.standalone, "group_max": g.group_max, "needed": g.needed,
                "reserved": g.reserved, "in_use": g.in_use, "shortfall": g.shortfall,
                "spare": g.spare,
            }
            for g in gaps
        ],
        "notes": notes,
    }
    return json.dumps(payload, indent=2)
