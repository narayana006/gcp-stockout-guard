import io
import json
import shlex
from pathlib import Path

import pytest

from gcp_stockout_guard import cli
from gcp_stockout_guard.advice import (
    AdviceFormatError,
    build_capacity_request,
    format_uptime,
    load_advice,
    parse_duration,
    parse_response,
    short_zone,
)
from gcp_stockout_guard.plan import (
    build_plan,
    rank_zones,
    reservation_command,
    risk_level,
)
from gcp_stockout_guard.stockouts import find_stockouts, load_entries, summarize

EXAMPLES = Path(__file__).parent.parent / "examples"
LOGS = EXAMPLES / "stockout_logs.json"
CAPACITY = EXAMPLES / "advice_capacity_us-central1.json"
CALENDAR = EXAMPLES / "advice_calendar_europe-west4.json"


@pytest.fixture
def groups():
    return summarize(find_stockouts(load_entries(LOGS.read_text(encoding="utf-8"))))


@pytest.fixture
def advice():
    merged = load_advice(CAPACITY.read_text(encoding="utf-8"))
    merged.calendar.extend(load_advice(CALENDAR.read_text(encoding="utf-8")).calendar)
    return merged


def run(argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# --- advice parsing --------------------------------------------------------


def test_capacity_response_gives_one_item_per_shard_with_recommendation_scores():
    response = {
        "recommendations": [
            {
                "scores": {"obtainability": 0.8, "estimatedUptime": "7200s"},
                "shards": [
                    {"zone": "zones/us-east1-b", "machineType": "n2-standard-4",
                     "instanceCount": 6, "provisioningModel": "SPOT"},
                    {"zone": "us-east1-c", "machineType": "n2-standard-4", "instanceCount": 4},
                ],
            }
        ]
    }
    spot = parse_response(response).spot
    assert [(s.zone, s.instance_count) for s in spot] == [("us-east1-b", 6), ("us-east1-c", 4)]
    assert all(s.obtainability == 0.8 and s.estimated_uptime_seconds == 7200 for s in spot)
    assert spot[0].provisioning_model == "SPOT" and spot[1].provisioning_model is None


def test_capacity_response_tolerates_missing_scores_and_bad_shards():
    response = {"recommendations": [{"shards": [{"zone": "us-east1-b", "machineType": "e2-small"},
                                                {"machineType": "no-zone"}, "junk"]}, "junk"]}
    (item,) = parse_response(response).spot
    assert (item.obtainability, item.estimated_uptime_seconds, item.instance_count) == (
        None, None, None,
    )


def test_calendar_response_reads_recommended_zone_window_and_other_zones(advice):
    (item,) = advice.calendar
    assert item.spec == "gpu-training"
    assert item.zone == "europe-west4-a"
    assert (item.start_time, item.end_time) == ("2026-12-01T09:00:00Z", "2026-12-08T09:00:00Z")
    assert item.recommendation_type == "FUTURE_RESERVATION"
    assert item.other_zones["europe-west4-b"][0] == "CONDITION_NOT_MET"


def test_calendar_response_without_location_has_no_zone():
    response = {"recommendations": [{"recommendationsPerSpec": {"spec-a": {"otherLocations": {}}}}]}
    (item,) = parse_response(response).calendar
    assert item.zone is None and item.other_zones == {}


def test_load_advice_accepts_a_list_and_rejects_other_shapes():
    assert len(load_advice(CAPACITY.read_text(encoding="utf-8")).spot) == 3
    assert load_advice("{}").spot == []
    for bad in ("not json", '"text"', '{"recommendations": 5}'):
        with pytest.raises(AdviceFormatError):
            load_advice(bad)


def test_small_helpers():
    assert short_zone("projects/p/zones/us-east1-b") == "us-east1-b"
    assert short_zone("") is None and short_zone(7) is None
    assert parse_duration("3.5s") == 3.5
    assert parse_duration("90") is None and parse_duration("xs") is None
    assert parse_duration(90) is None
    assert [format_uptime(v) for v in (None, 45, 600, 5400)] == ["-", "45s", "10m", "1.5h"]


def test_capacity_request_body_uses_documented_fields():
    body = build_capacity_request(
        ["n2-standard-8", "n2d-standard-8"], 20, ["us-central1-c"], "ANY_SINGLE_ZONE"
    )
    assert body == {
        "instanceProperties": {"scheduling": {"provisioningModel": "SPOT"}},
        "instanceFlexibilityPolicy": {
            "instanceSelections": {
                "selection-1": {"machineTypes": ["n2-standard-8", "n2d-standard-8"]}
            }
        },
        "size": 20,
        "distributionPolicy": {
            "zones": [{"zone": "zones/us-central1-c"}],
            "targetShape": "ANY_SINGLE_ZONE",
        },
    }
    assert "distributionPolicy" not in build_capacity_request(["e2-small"], 1)


@pytest.mark.parametrize(
    "args", [([], 1), (["e2-small"], 0), (["e2-small"], 1, None, "SPREAD")]
)
def test_capacity_request_rejects_bad_arguments(args):
    with pytest.raises(ValueError):
        build_capacity_request(*args)


# --- ranking and plan ------------------------------------------------------


def test_risk_levels():
    assert [risk_level(n) for n in (1, 2, 4, 5, 50)] == ["LOW", "MEDIUM", "MEDIUM", "HIGH", "HIGH"]


def test_ranking_from_logs_alone_prefers_most_suggested_zone(groups):
    ranked = rank_zones("n2-standard-8", groups, [])
    assert [o.zone for o in ranked] == [
        "us-central1-c", "us-central1-b", "us-central1-f", "us-central1-a",
    ]
    best, worst = ranked[0], ranked[-1]
    assert (best.suggested, best.stockouts, best.score) == (7, 0, 1.0)
    # b was suggested twice (2 / 7). f was suggested 3 times but also stocked out
    # once ((3 - 1) / 7), so they tie and the zone name decides.
    assert ranked[1].score == ranked[2].score == pytest.approx(2 / 7, abs=1e-4)
    assert worst.score == pytest.approx(-6 / 7, abs=1e-4)


def test_spot_advice_changes_the_order(groups, advice):
    ranked = rank_zones("n2-standard-8", groups, advice.spot)
    assert [o.zone for o in ranked][:3] == ["us-central1-c", "us-central1-b", "us-central1-f"]
    assert ranked[0].score == pytest.approx(1.9)
    assert ranked[0].spot_uptime_seconds == 21600


def test_lowest_obtainability_wins_when_a_zone_appears_twice(groups, advice):
    shard = {"zone": "us-central1-c", "machineType": "n2-standard-8"}
    extra = parse_response(
        {"recommendations": [{"scores": {"obtainability": 0.2}, "shards": [shard]}]}
    ).spot
    ranked = rank_zones("n2-standard-8", groups, advice.spot + extra)
    assert next(o for o in ranked if o.zone == "us-central1-c").spot_obtainability == 0.2


def test_spot_advice_for_other_machine_types_is_ignored(groups, advice):
    assert rank_zones("a2-highgpu-1g", groups, advice.spot)[0].zone == "europe-west4-b"


def test_plan_lists_fallbacks_and_never_the_stocked_out_zone(groups, advice):
    plan = build_plan(groups, advice, count=20, project="example-project")
    first, gpu, third = plan.items
    assert (first.zone, first.machine_type, first.errors, first.risk) == (
        "us-central1-a", "n2-standard-8", 6, "HIGH",
    )
    assert [o.zone for o in first.fallbacks] == ["us-central1-c", "us-central1-b", "us-central1-f"]
    assert gpu.fallbacks == [] and gpu.reservation_command is None
    assert "us-central1-f" not in [o.zone for o in third.fallbacks]
    assert shlex.split(first.reservation_command) == [
        "gcloud", "compute", "reservations", "create",
        "stockout-guard-n2-standard-8-us-central1-c",
        "--zone=us-central1-c", "--machine-type=n2-standard-8", "--vm-count=20",
        "--project=example-project",
    ]


def test_plan_without_count_has_no_reservation_command(groups):
    plan = build_plan(groups)
    assert all(item.reservation_command is None for item in plan.items)


def test_plan_can_be_limited_to_one_machine_type(groups, advice):
    plan = build_plan(groups, advice, machine_type="a2-highgpu-1g")
    assert [i.machine_type for i in plan.items] == ["a2-highgpu-1g"]
    assert plan.spot == []


def test_reservation_command_quotes_unusual_values():
    command = reservation_command("us-east1-b", "e2-small", 3, "my project")
    assert shlex.split(command)[-1] == "--project=my project"


# --- command line ----------------------------------------------------------


def test_cli_plan_text_marks_reservation_as_dry_run():
    code, out, _ = run(
        ["plan", "--logs", str(LOGS), "--advice", str(CAPACITY), "--advice", str(CALENDAR),
         "--count", "20"]
    )
    assert code == 0
    assert "DRY RUN, not executed" in out
    assert "risk HIGH" in out and "No fallback zone has supporting evidence" in out
    assert "gpu-training: recommended europe-west4-a" in out


def test_cli_plan_json_reads_logs_from_stdin():
    code, out, _ = run(["plan", "--logs", "-", "--format", "json"], LOGS.read_text())
    payload = json.loads(out)
    assert code == 0 and payload["dry_run"] is True
    assert payload["items"][0]["fallbacks"][0]["zone"] == "us-central1-c"
    assert payload["items"][0]["reservation_command"] is None
    assert payload["spot"] == [] and payload["calendar"] == []


def test_cli_plan_with_no_stockouts():
    code, out, _ = run(["plan", "--logs", "-"], "[]")
    assert code == 0 and "No stockout errors to plan around" in out


def test_cli_advice_text_and_json():
    code, out, _ = run(["advice", str(CAPACITY), str(CALENDAR)])
    assert code == 0
    lines = out.splitlines()
    assert lines[2].split() == ["us-central1-c", "n2-standard-8", "20", "0.90", "6.0h"]
    assert "not recommended: europe-west4-b (CONDITION_NOT_MET)" in out
    code, out, _ = run(["advice", str(CAPACITY), "--format", "json"])
    assert len(json.loads(out)["spot"]) == 3


def test_cli_capacity_request_prints_body():
    code, out, _ = run(["capacity-request", "--machine-type", "e2-small", "--count", "2"])
    assert code == 0 and json.loads(out)["size"] == 2


def test_cli_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    code, _, err = run(["advice", str(bad)])
    assert code == 2 and "bad.json" in err
    code, _, err = run(["plan", "--logs", str(tmp_path / "missing.json")])
    assert code == 2 and "cannot read" in err
    code, _, err = run(["plan", "--logs", str(LOGS), "--count", "0"])
    assert code == 2 and "--count" in err
    code, _, err = run(["capacity-request", "--machine-type", "e2-small", "--count", "0"])
    assert code == 2 and "size" in err


def test_cli_never_runs_a_subprocess(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise AssertionError("the tool must not execute commands")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    code, out, _ = run(["plan", "--logs", str(LOGS), "--advice", str(CAPACITY), "--count", "5"])
    assert code == 0 and "gcloud compute reservations create" in out
