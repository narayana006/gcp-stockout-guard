import io
import json
from pathlib import Path

import pytest

from gcp_stockout_guard import cli
from gcp_stockout_guard.reservations import InventoryFormatError, find_gaps, load_list

INVENTORY = Path(__file__).parent.parent / "examples" / "inventory"


def _load(name):
    return load_list((INVENTORY / f"{name}.json").read_text(encoding="utf-8"), name)


@pytest.fixture
def gaps_and_notes():
    return find_gaps(
        _load("instances"),
        _load("reservations"),
        _load("instance_groups"),
        _load("instance_templates"),
        _load("autoscalers"),
    )


def _by_zone(gaps):
    return {(g.zone, g.machine_type): g for g in gaps}


def test_zonal_group_uses_autoscaler_maximum(gaps_and_notes):
    gap = _by_zone(gaps_and_notes[0])[("us-central1-a", "n2-standard-8")]
    # 4 standalone + 3 in group "web" are running; spot and terminated VMs are ignored.
    assert (gap.running, gap.standalone, gap.group_max) == (7, 4, 12)
    assert (gap.needed, gap.reserved, gap.in_use, gap.shortfall) == (16, 10, 7, 6)


def test_regional_group_maximum_is_split_across_zones(gaps_and_notes):
    by_zone = _by_zone(gaps_and_notes[0])
    for zone in ("us-central1-b", "us-central1-c"):
        gap = by_zone[(zone, "n2-standard-8")]
        assert (gap.running, gap.standalone, gap.group_max, gap.needed) == (1, 0, 4, 4)
        # The CREATING reservation in us-central1-b is not counted.
        assert gap.reserved == 0 and gap.shortfall == 4


def test_spare_reservation_and_ordering(gaps_and_notes):
    gaps, notes = gaps_and_notes
    gpu = _by_zone(gaps)[("europe-west4-a", "a2-highgpu-1g")]
    assert (gpu.shortfall, gpu.spare) == (0, 2)
    assert [g.shortfall for g in gaps] == [6, 4, 4, 0]
    assert notes == ["group batch: template batch-old not found in --templates"]


def test_without_groups_running_vms_are_the_demand():
    gaps, notes = find_gaps(_load("instances"), _load("reservations"))
    gap = _by_zone(gaps)[("us-central1-a", "n2-standard-8")]
    assert (gap.needed, gap.shortfall) == (7, 0)
    assert notes == []


def test_autoscaler_off_falls_back_to_target_size():
    groups = [
        {
            "name": "g",
            "selfLink": "projects/p/zones/z-a/instanceGroupManagers/g",
            "zone": "zones/z-a",
            "instanceTemplate": "global/instanceTemplates/t",
            "targetSize": 5,
            "autoscaler": {"autoscalingPolicy": {"mode": "OFF", "maxNumReplicas": 50}},
        }
    ]
    templates = [{"name": "t", "properties": {"machineType": "e2-medium"}}]
    gaps, _ = find_gaps([], [], groups, templates)
    assert gaps[0].group_max == 5


def test_spot_template_is_skipped():
    groups = [
        {
            "name": "g",
            "zone": "zones/z-a",
            "instanceTemplate": "t",
            "targetSize": 5,
        }
    ]
    templates = [
        {"name": "t", "properties": {"machineType": "e2-medium",
                                     "scheduling": {"provisioningModel": "SPOT"}}}
    ]
    assert find_gaps([], [], groups, templates) == ([], [])


def test_load_list_rejects_non_list():
    with pytest.raises(InventoryFormatError):
        load_list('{"items": []}', "instances")
    with pytest.raises(InventoryFormatError):
        load_list("not json", "instances")
    assert load_list("  ", "instances") == []


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


FULL_ARGS = (
    "reservation-gaps",
    "--instances", str(INVENTORY / "instances.json"),
    "--reservations", str(INVENTORY / "reservations.json"),
    "--groups", str(INVENTORY / "instance_groups.json"),
    "--templates", str(INVENTORY / "instance_templates.json"),
    "--autoscalers", str(INVENTORY / "autoscalers.json"),
)


def test_cli_text_and_fail_on_gap():
    code, out, _ = _run(*FULL_ARGS)
    assert code == 0
    assert out.startswith("3 of 4 zone and machine type pair(s) need more reserved VMs (14 VM(s)")
    row = "us-central1-a   n2-standard-8  7        12         16      10        7       short 6"
    assert row in out
    assert "spare 2" in out
    code, _, _ = _run(*FULL_ARGS, "--fail-on-gap")
    assert code == 1


def test_cli_json():
    code, out, _ = _run(*FULL_ARGS, "--format", "json")
    payload = json.loads(out)
    assert code == 0 and payload["total_shortfall"] == 14
    assert payload["gaps"][0]["zone"] == "us-central1-a"


def test_cli_groups_need_templates(tmp_path):
    code, _, err = _run(
        "reservation-gaps",
        "--instances", str(INVENTORY / "instances.json"),
        "--reservations", str(INVENTORY / "reservations.json"),
        "--groups", str(INVENTORY / "instance_groups.json"),
    )
    assert code == 2 and "--templates" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    code, _, err = _run("reservation-gaps", "--instances", str(bad), "--reservations", str(bad))
    assert code == 2 and "expected a JSON list" in err
