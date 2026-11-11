import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gcp_stockout_guard.cli import main
from gcp_stockout_guard.stockouts import (
    LogFormatError,
    find_stockouts,
    load_entries,
    parse_entry,
    parse_timestamp,
    render_text,
    summarize,
)

FIXTURE = Path(__file__).parent / "fixtures" / "audit_log.json"


@pytest.fixture
def entries():
    return load_entries(FIXTURE.read_text(encoding="utf-8"))


def run(argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_structured_details_give_zone_machine_type_and_alternatives(entries):
    event = parse_entry(entries[0])
    assert event.zone == "us-central1-a"
    assert event.machine_type == "n2-standard-8"
    assert event.suggested_zones == ("us-central1-c", "us-central1-f")
    assert event.timestamp == datetime(2026, 11, 8, 14, 2, 11, 123456, tzinfo=timezone.utc)


def test_plain_message_falls_back_to_resource_and_request_paths(entries):
    event = parse_entry(entries[2])
    assert event.zone == "europe-west4-b"
    assert event.machine_type == "a2-highgpu-1g"
    assert event.suggested_zones == ()


def test_machine_type_and_zone_read_from_unavailable_message(entries):
    event = parse_entry(entries[3])
    assert (event.zone, event.machine_type) == ("us-central1-a", "n2-standard-8")


def test_other_errors_and_successes_are_ignored(entries):
    assert parse_entry(entries[4]) is None
    assert parse_entry(entries[5]) is None
    assert len(find_stockouts(entries)) == 4


def test_unknown_when_entry_names_no_zone_or_machine_type():
    event = parse_entry({"textPayload": "ZONE_RESOURCE_POOL_EXHAUSTED"})
    assert (event.zone, event.machine_type, event.timestamp) == ("unknown", "unknown", None)


def test_summary_counts_per_zone_and_machine_type(entries):
    groups = summarize(find_stockouts(entries))
    assert [(g.zone, g.machine_type, g.count) for g in groups] == [
        ("us-central1-a", "n2-standard-8", 3),
        ("europe-west4-b", "a2-highgpu-1g", 1),
    ]
    top = groups[0]
    assert top.first_seen == datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)
    assert top.last_seen == datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
    # us-central1-c was suggested twice, us-central1-f once.
    assert top.suggested_zones == ["us-central1-c", "us-central1-f"]


def test_suggested_zones_never_include_the_exhausted_zone():
    entry = {
        "status": {
            "message": "ZONE_RESOURCE_POOL_EXHAUSTED",
            "metadatas": {
                "zone": "us-west1-a",
                "vmType": "e2-medium",
                "zonesAvailable": "us-west1-a, us-west1-b",
            },
        }
    }
    assert parse_entry(entry).suggested_zones == ("us-west1-b",)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-08T14:02:11Z", datetime(2026, 11, 8, 14, 2, 11, tzinfo=timezone.utc)),
        (
            "2026-11-08T16:02:11.5+02:00",
            datetime(2026, 11, 8, 14, 2, 11, 500000, tzinfo=timezone.utc),
        ),
        ("not a time", None),
        (None, None),
    ],
)
def test_parse_timestamp(text, expected):
    assert parse_timestamp(text) == expected


def test_load_entries_accepts_one_json_object_per_line():
    text = '{"textPayload": "ZONE_RESOURCE_POOL_EXHAUSTED"}\n\n{"textPayload": "ok"}\n'
    assert len(load_entries(text)) == 2
    assert load_entries("") == []


def test_load_entries_rejects_other_shapes():
    with pytest.raises(LogFormatError):
        load_entries('["just", "strings"]')
    with pytest.raises(LogFormatError, match="line 2"):
        load_entries('{"a": 1}\nnot json\n')


def test_text_report_for_no_stockouts():
    assert render_text([], 7) == "No stockout errors found in 7 log entries."


def test_cli_text_report_from_file():
    code, out, err = run(["stockouts", str(FIXTURE)])
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == "4 stockout errors across 2 zone and machine type pairs (6 log entries read)"
    assert lines[2].split() == [
        "ZONE",
        "MACHINE",
        "TYPE",
        "ERRORS",
        "FIRST",
        "SEEN",
        "(UTC)",
        "LAST",
        "SEEN",
        "(UTC)",
        "GOOGLE",
        "SUGGESTS",
    ]
    assert lines[3].split() == [
        "us-central1-a",
        "n2-standard-8",
        "3",
        "2026-11-07",
        "09:30",
        "2026-11-08",
        "15:00",
        "us-central1-c,",
        "us-central1-f",
    ]
    assert lines[4].split()[:3] == ["europe-west4-b", "a2-highgpu-1g", "1"]
    assert lines[4].endswith("-")


def test_cli_json_report_from_stdin():
    code, out, _ = run(["stockouts", "--format", "json"], FIXTURE.read_text(encoding="utf-8"))
    payload = json.loads(out)
    assert code == 0
    assert payload["entries_read"] == 6
    assert payload["stockout_errors"] == 4
    assert payload["groups"][0] == {
        "zone": "us-central1-a",
        "machine_type": "n2-standard-8",
        "errors": 3,
        "first_seen": "2026-11-07T09:30:00+00:00",
        "last_seen": "2026-11-08T15:00:00+00:00",
        "suggested_zones": ["us-central1-c", "us-central1-f"],
    }


def test_cli_fail_on_stockout_sets_exit_status():
    assert run(["stockouts", str(FIXTURE), "--fail-on-stockout"])[0] == 1
    assert run(["stockouts", "--fail-on-stockout"], "[]")[0] == 0


def test_cli_reports_bad_input_without_traceback(tmp_path):
    code, out, err = run(["stockouts"], '"oops"')
    assert code == 2 and out == "" and err.startswith("error: expected a JSON list")
    code, _, err = run(["stockouts", str(tmp_path / "missing.json")])
    assert code == 2 and "cannot read" in err
