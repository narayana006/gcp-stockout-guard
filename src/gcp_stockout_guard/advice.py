"""Read saved responses from Compute Engine's capacity advice methods.

Two response shapes are understood:

* ``advice.capacity`` (beta, Spot VMs only): ``recommendations[].scores`` with
  ``obtainability`` and ``estimatedUptime``, plus ``recommendations[].shards[]``
  naming the zone, machine type and instance count.
* ``advice.calendarMode``: ``recommendations[].recommendationsPerSpec``, a map
  from the caller's spec key to a recommended zone and time window, plus the
  zones that were considered and not recommended.

Nothing here calls Google Cloud. You save a response to a file yourself and
this module analyses it. Field names follow the public REST reference; fields
this module does not know are ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .stockouts import parse_timestamp

SPOT = "SPOT"
TARGET_SHAPES = ("ANY", "ANY_SINGLE_ZONE", "BALANCED")


class AdviceFormatError(ValueError):
    """The input is not a saved advice response."""


@dataclass(frozen=True)
class SpotAdvice:
    """One shard of an ``advice.capacity`` recommendation.

    The scores belong to the whole recommendation; every shard of that
    recommendation carries the same values.
    """

    zone: str
    machine_type: str
    instance_count: int | None
    obtainability: float | None
    estimated_uptime_seconds: float | None
    provisioning_model: str | None = None


@dataclass(frozen=True)
class CalendarAdvice:
    """One entry of ``recommendationsPerSpec`` in an ``advice.calendarMode`` response."""

    spec: str
    zone: str | None  # None when no zone was recommended
    start_time: str | None
    end_time: str | None
    recommendation_type: str | None = None
    recommendation_id: str | None = None
    # zone -> (status, details) for zones that were considered but not recommended
    other_zones: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class Advice:
    spot: list[SpotAdvice] = field(default_factory=list)
    calendar: list[CalendarAdvice] = field(default_factory=list)


def short_zone(value: Any) -> str | None:
    """``zones/us-central1-a`` or a full zone URL -> ``us-central1-a``."""
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def parse_duration(value: Any) -> float | None:
    """Parse a protobuf Duration in JSON form, such as ``"12600s"`` or ``"3.5s"``."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        seconds = float(value[:-1])
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    # int64 fields arrive as strings in Google's JSON; int32 fields as numbers.
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_response(data: Any) -> Advice:
    """Parse one saved response of either advice method."""
    if not isinstance(data, dict):
        raise AdviceFormatError("expected a JSON object with a 'recommendations' list")
    recommendations = data.get("recommendations", [])
    if not isinstance(recommendations, list):
        raise AdviceFormatError("'recommendations' is not a list")
    advice = Advice()
    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            continue
        per_spec = recommendation.get("recommendationsPerSpec")
        if isinstance(per_spec, dict):
            for spec, item in per_spec.items():
                if isinstance(item, dict):
                    advice.calendar.append(_calendar_item(str(spec), item))
            continue
        scores = recommendation.get("scores")
        scores = scores if isinstance(scores, dict) else {}
        for shard in recommendation.get("shards") or []:
            if not isinstance(shard, dict):
                continue
            zone = short_zone(shard.get("zone"))
            machine_type = shard.get("machineType")
            if not zone or not isinstance(machine_type, str):
                continue
            advice.spot.append(
                SpotAdvice(
                    zone=zone,
                    machine_type=machine_type.rsplit("/", 1)[-1],
                    instance_count=_int(shard.get("instanceCount")),
                    obtainability=_number(scores.get("obtainability")),
                    estimated_uptime_seconds=parse_duration(scores.get("estimatedUptime")),
                    provisioning_model=shard.get("provisioningModel")
                    if isinstance(shard.get("provisioningModel"), str)
                    else None,
                )
            )
    return advice


def _calendar_item(spec: str, item: dict[str, Any]) -> CalendarAdvice:
    others: dict[str, tuple[str, str]] = {}
    raw_others = item.get("otherLocations")
    if isinstance(raw_others, dict):
        for location, info in raw_others.items():
            zone = short_zone(location)
            if zone and isinstance(info, dict):
                others[zone] = (str(info.get("status", "")), str(info.get("details", "")))

    def text(key: str) -> str | None:
        value = item.get(key)
        return value if isinstance(value, str) and value else None

    return CalendarAdvice(
        spec=spec,
        zone=short_zone(item.get("location")),
        start_time=text("startTime"),
        end_time=text("endTime"),
        recommendation_type=text("recommendationType"),
        recommendation_id=text("recommendationId"),
        other_zones=others,
    )


def load_advice(text: str) -> Advice:
    """Parse a file holding one saved response, or a JSON list of them."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdviceFormatError(f"not valid JSON: {exc.msg}") from exc
    responses = data if isinstance(data, list) else [data]
    merged = Advice()
    for response in responses:
        parsed = parse_response(response)
        merged.spot.extend(parsed.spot)
        merged.calendar.extend(parsed.calendar)
    return merged


def build_capacity_request(
    machine_types: list[str],
    size: int,
    zones: list[str] | None = None,
    target_shape: str | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``advice.capacity``; it is printed, never sent.

    POST it yourself to
    ``https://compute.googleapis.com/compute/beta/projects/PROJECT/regions/REGION/advice/capacity``.
    """
    if not machine_types:
        raise ValueError("at least one machine type is required")
    if size < 1:
        raise ValueError("size must be at least 1")
    if target_shape is not None and target_shape not in TARGET_SHAPES:
        raise ValueError(f"target shape must be one of {', '.join(TARGET_SHAPES)}")
    body: dict[str, Any] = {
        "instanceProperties": {"scheduling": {"provisioningModel": SPOT}},
        "instanceFlexibilityPolicy": {
            "instanceSelections": {"selection-1": {"machineTypes": list(machine_types)}}
        },
        "size": size,
    }
    distribution: dict[str, Any] = {}
    if zones:
        distribution["zones"] = [{"zone": f"zones/{short_zone(z)}"} for z in zones]
    if target_shape:
        distribution["targetShape"] = target_shape
    if distribution:
        body["distributionPolicy"] = distribution
    return body


def format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def format_window(start: str | None, end: str | None) -> str:
    def fmt(value: str | None) -> str:
        parsed = parse_timestamp(value)
        return parsed.strftime("%Y-%m-%d %H:%M") if parsed else "?"

    if not start and not end:
        return "-"
    return f"{fmt(start)} to {fmt(end)} UTC"
