import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gcp_stockout_guard import cli, live
from gcp_stockout_guard.advice import (
    build_calendar_request,
    load_advice,
    parse_duration_arg,
    parse_time_arg,
)
from gcp_stockout_guard.live import LiveClient

EXAMPLES = Path(__file__).parent.parent / "examples"
START = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text,seconds",
    [("7d", 604800), ("36h", 129600), ("90m", 5400), ("3600s", 3600), ("12", 43200),
     ("1.5d", 129600), (" 2H ", 7200)],
)
def test_parse_duration_arg(text, seconds):
    assert parse_duration_arg(text) == seconds


@pytest.mark.parametrize("text", ["", "d", "soon", "0h", "-3d", "inf", "nanh"])
def test_parse_duration_arg_rejects(text):
    with pytest.raises(ValueError):
        parse_duration_arg(text)


def test_parse_time_arg_defaults_to_utc_and_converts_offsets():
    assert parse_time_arg("2026-12-01") == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert parse_time_arg("2026-12-01T09:00Z") == START
    assert parse_time_arg("2026-12-01T10:00+01:00") == START
    with pytest.raises(ValueError):
        parse_time_arg("next tuesday")


def test_build_calendar_request_minimal():
    body = build_calendar_request("a3-highgpu-8g", 4, START, 7 * 86400)
    assert body == {
        "futureResourcesSpecs": {
            "request-1": {
                "targetResources": {
                    "specificSkuResources": {"machineType": "a3-highgpu-8g", "instanceCount": "4"}
                },
                "timeRangeSpec": {
                    "startTimeNotEarlierThan": "2026-12-01T09:00:00Z",
                    "startTimeNotLaterThan": "2026-12-01T09:00:00Z",
                    "minDuration": "604800s",
                    "maxDuration": "604800s",
                },
            }
        }
    }


def test_build_calendar_request_with_window_and_zones():
    body = build_calendar_request(
        "a3-highgpu-8g", 2, START, 86400,
        start_not_later_than=datetime(2026, 12, 3, tzinfo=timezone.utc),
        max_duration_seconds=3 * 86400,
        allow_zones=["us-central1-a", "zones/us-central1-b"],
        deny_zones=["us-central1-c"],
        spec="gpu-training",
    )
    spec = body["futureResourcesSpecs"]["gpu-training"]
    assert spec["timeRangeSpec"]["startTimeNotLaterThan"] == "2026-12-03T00:00:00Z"
    assert spec["timeRangeSpec"]["maxDuration"] == "259200s"
    assert spec["locationPolicy"]["locations"] == {
        "zones/us-central1-a": {"preference": "ALLOW"},
        "zones/us-central1-b": {"preference": "ALLOW"},
        "zones/us-central1-c": {"preference": "DENY"},
    }


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"count": 0}, "count"),
        ({"start_not_later_than": datetime(2026, 11, 1, tzinfo=timezone.utc)}, "before"),
        ({"max_duration_seconds": 60}, "shorter"),
        ({"allow_zones": ["us-central1-a"], "deny_zones": ["zones/us-central1-a"]}, "both"),
        ({"spec": ""}, "spec"),
    ],
)
def test_build_calendar_request_rejects(kwargs, message):
    args = {"machine_type": "a3-highgpu-8g", "count": 1,
            "start_not_earlier_than": START, "min_duration_seconds": 3600, **kwargs}
    with pytest.raises(ValueError, match=message):
        build_calendar_request(**args)


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.response


def run_cli(argv, transport):
    client = LiveClient(transport=transport, token=lambda: "test-token")
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdin=io.StringIO(), stdout=out, stderr=err, client=client)
    return code, out.getvalue(), err.getvalue()


def test_cli_fetch_calendar_sends_body_and_output_feeds_advice():
    response = json.loads(
        (EXAMPLES / "advice_calendar_europe-west4.json").read_text(encoding="utf-8")
    )
    transport = FakeTransport(response)
    argv = ["fetch-calendar", "--project", "example-project", "--machine-type",
            "a2-highgpu-1g", "--count", "8", "--start", "2026-12-01T09:00",
            "--latest-start", "2026-12-05", "--duration", "7d",
            "--zone", "europe-west4-a", "--zone", "europe-west4-b",
            "--spec", "gpu-training"]
    code, out, err = run_cli(argv, transport)
    assert code == 0, err
    url, body, headers = transport.calls[0]
    assert url == live.CALENDAR_URL.format(project="example-project", region="europe-west4")
    assert "/compute/v1/" in url and url.endswith("/advice/calendarMode")
    assert headers["Authorization"] == "Bearer test-token"
    spec = body["futureResourcesSpecs"]["gpu-training"]
    assert spec["targetResources"]["specificSkuResources"]["instanceCount"] == "8"
    assert spec["timeRangeSpec"]["startTimeNotLaterThan"] == "2026-12-05T00:00:00Z"
    # The printed response parses with the existing advice reader.
    calendar = load_advice(out).calendar
    assert [(c.spec, c.zone) for c in calendar] == [("gpu-training", "europe-west4-a")]


def test_cli_fetch_calendar_region_from_excluded_zone():
    transport = FakeTransport({"recommendations": []})
    argv = ["fetch-calendar", "--project", "p", "--machine-type", "a2-highgpu-1g",
            "--count", "1", "--start", "2026-12-01", "--duration", "1d",
            "--exclude-zone", "us-east4-b"]
    code, _, _ = run_cli(argv, transport)
    assert code == 0
    assert "/regions/us-east4/" in transport.calls[0][0]


@pytest.mark.parametrize(
    "extra,message",
    [
        ([], "--region"),
        (["--region", "us-east4", "--duration", "soon"], "duration"),
        (["--region", "us-east4", "--latest-start", "2026-11-01"], "before"),
    ],
)
def test_cli_fetch_calendar_input_errors_send_nothing(extra, message):
    transport = FakeTransport({})
    argv = ["fetch-calendar", "--project", "p", "--machine-type", "a2-highgpu-1g",
            "--count", "1", "--start", "2026-12-01", "--duration", "1d", *extra]
    code, _, err = run_cli(argv, transport)
    assert code == 2 and message in err and transport.calls == []
