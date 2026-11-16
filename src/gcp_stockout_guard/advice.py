"""Read saved responses from Compute Engine's capacity advice methods.

Two response shapes are understood:

* ``advice.capacity`` (beta, Spot VMs only): ``recommendations[].scores`` with
  ``obtainability`` and ``estimatedUptime``, plus ``recommendations[].shards[]``
  naming the zone, machine type and instance count.
* ``advice.calendarMode``: ``recommendations[].recommendationsPerSpec``, a map
  from the caller's spec key to a recommended zone and time window, plus the
  zones that were considered and not recommended.

Nothing here calls Google Cloud. This module parses saved responses and builds
request bodies; ``live`` sends them. Field names follow the public REST reference; fields
this module does not know are ignored.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration_arg(value: str) -> int:
    """``7d``, ``36h``, ``90m`` or ``3600s`` -> seconds; a bare number is hours."""
    text = value.strip().lower()
    unit = text[-1:] if text[-1:] in _DURATION_UNITS else "h"
    number = text[:-1] if text[-1:] in _DURATION_UNITS else text
    try:
        amount = float(number)
    except ValueError:
        raise ValueError(f"cannot read duration {value!r}; use e.g. 7d, 36h or 90m") from None
    if not math.isfinite(amount):
        raise ValueError(f"cannot read duration {value!r}; use e.g. 7d, 36h or 90m")
    seconds = int(round(amount * _DURATION_UNITS[unit]))
    if seconds < 1:
        raise ValueError(f"duration {value!r} must be greater than 0")
    return seconds


def parse_time_arg(value: str) -> datetime:
    """``2026-12-01`` or ``2026-12-01T09:00`` (UTC unless an offset is given)."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"cannot read time {value!r}; use e.g. 2026-12-01 or 2026-12-01T09:00"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rfc3339(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_calendar_request(
    machine_type: str,
    count: int,
    start_not_earlier_than: datetime,
    min_duration_seconds: int,
    start_not_later_than: datetime | None = None,
    max_duration_seconds: int | None = None,
    allow_zones: list[str] | None = None,
    deny_zones: list[str] | None = None,
    spec: str = "request-1",
) -> dict[str, Any]:
    """Build the JSON body for ``advice.calendarMode`` (one spec, one machine type).

    Field names follow ``CalendarModeAdviceRequest`` in the Compute Engine v1
    discovery document. The method only returns advice; it reserves nothing.
    """
    if not machine_type:
        raise ValueError("a machine type is required")
    if count < 1:
        raise ValueError("count must be at least 1")
    if not spec:
        raise ValueError("the spec name must not be empty")
    latest = start_not_later_than or start_not_earlier_than
    if latest < start_not_earlier_than:
        raise ValueError("the latest start time is before the earliest start time")
    if min_duration_seconds < 1:
        raise ValueError("the minimum duration must be greater than 0")
    longest = max_duration_seconds or min_duration_seconds
    if longest < min_duration_seconds:
        raise ValueError("the maximum duration is shorter than the minimum duration")
    allow = [short_zone(z) for z in allow_zones or []]
    deny = [short_zone(z) for z in deny_zones or []]
    clash = sorted(set(allow) & set(deny))
    if clash:
        raise ValueError(f"zone(s) both allowed and excluded: {', '.join(clash)}")

    resource_spec: dict[str, Any] = {
        "targetResources": {
            "specificSkuResources": {"machineType": machine_type, "instanceCount": str(count)}
        },
        "timeRangeSpec": {
            "startTimeNotEarlierThan": _rfc3339(start_not_earlier_than),
            "startTimeNotLaterThan": _rfc3339(latest),
            "minDuration": f"{min_duration_seconds}s",
            "maxDuration": f"{longest}s",
        },
    }
    locations = {f"zones/{z}": {"preference": "ALLOW"} for z in allow}
    locations.update({f"zones/{z}": {"preference": "DENY"} for z in deny})
    if locations:
        resource_spec["locationPolicy"] = {"locations": locations}
    return {"futureResourcesSpecs": {spec: resource_spec}}


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
