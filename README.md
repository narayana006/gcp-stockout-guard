# gcp-stockout-guard

Find out which Google Cloud zones and machine types are running out of capacity, from the stockout errors already sitting in your Compute Engine logs, and get a ranked list of zones to fall back to.

> **Status: early, offline only.** Four commands work today, all on files you export yourself: `stockouts` (count `ZONE_RESOURCE_POOL_EXHAUSTED` errors per zone and machine type), `advice` (summarise saved `advice.capacity` and `advice.calendarMode` responses), `plan` (rank fallback zones and print a fallback plan, with any reservation shown as a dry-run command) and `capacity-request` (print an `advice.capacity` request body). The tool **never calls Google Cloud and never creates a reservation**. Live API calls, reservation automation and alerting are not built; see [Roadmap](#roadmap).

## Quick start

Requires Python 3.10 or newer. There are no runtime dependencies.

```bash
pip install git+https://github.com/narayana006/gcp-stockout-guard
```

Clone the repository for the invented sample files in [`examples/`](examples), then ask for a fallback plan for 20 VMs:

```bash
gcp-stockout-guard plan --logs examples/stockout_logs.json \
  --advice examples/advice_capacity_us-central1.json \
  --advice examples/advice_calendar_europe-west4.json \
  --count 20 --project example-project
```

```
Fallback plan for 3 zone and machine type pair(s) that ran out of capacity (11 log entries read)

1. n2-standard-8 in us-central1-a: 6 stockout error(s), risk HIGH, last seen 2026-11-08 14:31 UTC
   Fallback zones, best first:
   #  ZONE           SCORE  EVIDENCE
   1  us-central1-c  1.90   suggested by Google in 7 error(s); Spot obtainability 0.90
   2  us-central1-b  0.99   suggested by Google in 2 error(s); Spot obtainability 0.70
   3  us-central1-f  0.69   suggested by Google in 3 error(s); Spot obtainability 0.40; 1 stockout error(s) of its own
   DRY RUN, not executed. To reserve 20 on-demand VM(s) in us-central1-c, review and run:
     gcloud compute reservations create stockout-guard-n2-standard-8-us-central1-c --zone=us-central1-c --machine-type=n2-standard-8 --vm-count=20 --project=example-project

2. a2-highgpu-1g in europe-west4-b: 2 stockout error(s), risk MEDIUM, last seen 2026-11-07 22:40 UTC
   No fallback zone has supporting evidence. Try another machine type or region.

3. n2-standard-8 in us-central1-f: 1 stockout error(s), risk LOW, last seen 2026-11-06 16:20 UTC
   Fallback zones, best first:
   #  ZONE           SCORE  EVIDENCE
   1  us-central1-c  1.90   suggested by Google in 7 error(s); Spot obtainability 0.90
   2  us-central1-b  0.99   suggested by Google in 2 error(s); Spot obtainability 0.70
   DRY RUN, not executed. To reserve 20 on-demand VM(s) in us-central1-c, review and run:
     gcloud compute reservations create stockout-guard-n2-standard-8-us-central1-c --zone=us-central1-c --machine-type=n2-standard-8 --vm-count=20 --project=example-project

Spot capacity advice (advice.capacity, beta, Spot VMs only)
ZONE           MACHINE TYPE   VMS  OBTAINABILITY  EST. UPTIME
us-central1-c  n2-standard-8  20   0.90           6.0h
us-central1-b  n2-standard-8  20   0.70           4.0h
us-central1-f  n2-standard-8  20   0.40           1.5h

Calendar-mode advice (advice.calendarMode, future reservations)
  gpu-training: recommended europe-west4-a, 2026-12-01 09:00 to 2026-12-08 09:00 UTC
    not recommended: europe-west4-b (CONDITION_NOT_MET) - No capacity for the requested time window.
```

With your own project, export the last 7 days of stockout errors with `gcloud` and pipe them in (the tool itself runs nothing; you run the export):

```bash
gcloud logging read '"ZONE_RESOURCE_POOL_EXHAUSTED"' --project my-project --freshness=7d --format=json | gcp-stockout-guard plan --logs -
```

## The problem

When a Google Cloud zone runs out of a machine type, VM creation and autoscaling fail with `ZONE_RESOURCE_POOL_EXHAUSTED`. Teams usually find out during a scale-up or an incident. The errors are in Cloud Logging, but nobody reads them until something breaks, and the zones Google suggests as alternatives are buried inside each error.

This tool is for SRE and platform teams running workloads on Compute Engine or GKE who have been hit by stockouts, or who must guarantee capacity for a launch, a peak or GPU work.

## Commands

Every command reads files or standard input and prints text (or JSON with `--format json`). Exit status is 0 on success and 2 when an input cannot be read or understood.

### `stockouts`: what ran out, and where

```
gcp-stockout-guard stockouts [FILE] [--format text|json] [--fail-on-stockout]
```

```bash
gcp-stockout-guard stockouts examples/stockout_logs.json
```

```
9 stockout errors across 3 zone and machine type pairs (11 log entries read)

ZONE            MACHINE TYPE   ERRORS  FIRST SEEN (UTC)  LAST SEEN (UTC)   GOOGLE SUGGESTS
us-central1-a   n2-standard-8  6       2026-11-03 08:15  2026-11-08 14:31  us-central1-c, us-central1-f, us-central1-b
europe-west4-b  a2-highgpu-1g  2       2026-11-06 22:15  2026-11-07 22:40  -
us-central1-f   n2-standard-8  1       2026-11-06 16:20  2026-11-06 16:20  us-central1-c
```

`FILE` is the output of `gcloud logging read --format=json` (a JSON array of log entries) or one JSON entry per line; `-` or no argument reads standard input. `--fail-on-stockout` exits with status 1 if any stockout is found, for cron or CI.

- **What counts as a stockout.** Any entry containing `ZONE_RESOURCE_POOL_EXHAUSTED` (including `ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`) or the message "does not have enough resources available to fulfill the request". Other errors, such as `QUOTA_EXCEEDED`, are ignored.
- **Zone and machine type** come from the error's structured details (`zone`, `vmType`) when present; otherwise from the entry's resource labels, resource paths such as `zones/us-central1-a` and `machineTypes/n2-standard-8`, or the error message. If the entry names neither, the value is `unknown`.
- **Google suggests** is the `zonesAvailable` list in the error's structured details, most frequently suggested zone first. A `-` means the errors carried no suggestions.

### `advice`: read saved capacity advice

```
gcp-stockout-guard advice FILE [FILE ...] [--format text|json]
```

```bash
gcp-stockout-guard advice examples/advice_capacity_us-central1.json examples/advice_calendar_europe-west4.json
```

prints the two advice tables shown at the end of the quick start output.

Each file holds one saved response, or a JSON list of responses, from either method. The kind is detected from its shape:

| Method | Fields read from the response |
| --- | --- |
| `advice.capacity` | `recommendations[].scores.obtainability`, `recommendations[].scores.estimatedUptime` (a duration such as `"21600s"`), and `recommendations[].shards[]` with `zone`, `machineType`, `instanceCount`, `provisioningModel` |
| `advice.calendarMode` | `recommendations[].recommendationsPerSpec.<your spec key>` with `location`, `startTime`, `endTime`, `recommendationType`, and `otherLocations.<zone>.status` / `.details` |

Scores in an `advice.capacity` response belong to a whole recommendation, so every zone (shard) in that recommendation is shown with the same score. To compare zones, ask about one zone per request and save each response. Unknown fields are ignored and `status` values are printed as they arrive, not interpreted.

### `plan`: ranked fallback zones

```
gcp-stockout-guard plan --logs FILE [--advice FILE ...] [--machine-type TYPE] [--count N] [--project ID] [--format text|json]
```

For each zone and machine type that stocked out, the plan lists the other zones there is evidence about, best first.

**Score.** For a machine type with *E* stockout errors in the logs, each zone gets

```
score = (errors that suggested this zone) / E
      + Spot obtainability for this zone and machine type (0 if no advice was given)
      - (stockout errors in this zone) / E
```

Zones with a score of zero or less are left out, ties go to the zone name, and when several advice files mention the same zone the lowest obtainability is used. The arithmetic is deliberately simple so you can check it: in the example, `us-central1-f` scores 3/7 + 0.40 - 1/7 = 0.69.

**Risk** is a label from the error count alone: HIGH from 5 errors, MEDIUM from 2, LOW for 1.

**Reservation step.** With `--count N`, each item with a fallback gets a `gcloud compute reservations create` command for the best zone, marked `DRY RUN, not executed`. The tool only prints it. A reservation is billed whether or not you use it, so read the command, check the price and run it yourself if you want it. JSON output always carries `"dry_run": true`.

Calendar-mode advice is listed under the plan but does not affect the ranking: the response names your spec key, not a machine type, so it cannot be matched to a log entry reliably.

### `capacity-request`: print an `advice.capacity` request body

```
gcp-stockout-guard capacity-request --machine-type TYPE [--machine-type TYPE ...] --count N [--zone ZONE ...] [--target-shape ANY|ANY_SINGLE_ZONE|BALANCED]
```

```bash
gcp-stockout-guard capacity-request --machine-type n2-standard-8 --count 20 --zone us-central1-c --target-shape ANY_SINGLE_ZONE
```

```json
{
  "instanceProperties": {
    "scheduling": {
      "provisioningModel": "SPOT"
    }
  },
  "instanceFlexibilityPolicy": {
    "instanceSelections": {
      "selection-1": {
        "machineTypes": [
          "n2-standard-8"
        ]
      }
    }
  },
  "size": 20,
  "distributionPolicy": {
    "zones": [
      {
        "zone": "zones/us-central1-c"
      }
    ],
    "targetShape": "ANY_SINGLE_ZONE"
  }
}
```

This prints the body and sends nothing. The method is `POST https://compute.googleapis.com/compute/beta/projects/PROJECT/regions/REGION/advice/capacity` and needs the `compute.advice.capacity` permission; send the body with your own authenticated client and save the response for `advice` and `plan`.

## What Google's APIs can and cannot tell you

- **Spot VMs:** `advice.capacity` (Capacity Advisor) returns an obtainability score from 0.0 to 1.0 and an estimated uptime before preemption. It covers Spot VMs only and is in beta. Google describes the uptime as a best-effort estimate from historical data.
- **GPUs, TPUs and H4D:** `advice.calendarMode` says which zone has capacity for a future time window, so a calendar-mode future reservation request there is likely to be approved.
- **Standard on-demand VMs:** Google publishes no forward-looking availability score. Stockouts can only be detected after they start, from error logs, and reduced with reservations.

That is why the plan treats Spot obtainability as one hint among others. A high Spot score does not promise on-demand capacity in that zone.

## Limitations

- Offline only. You export logs and save advice responses; the tool does not fetch them.
- Stockout detection is after the fact. It tells you where stockouts have already happened.
- The advice files in `examples/` are **illustrative**: written by hand from the field names in Google's public REST reference, not recorded from a real project. The parser has not been run against live responses, and `advice.capacity` is a beta method whose shape may change. The log parser is likewise tested against sample entries modelled on Compute Engine audit logs. If something of yours is parsed wrongly or shows up as `unknown`, please open an issue with a redacted copy.
- The ranking only knows zones that appear in your logs or advice files. A zone nobody mentioned is not ranked, however much capacity it has.
- A suggested zone is Google's hint at the time of the error. Capacity can change before you act on it.
- The printed reservation command covers a plain on-demand reservation (zone, machine type, VM count). It does not add GPUs, local SSDs, sharing or commitments, and no calendar-mode future reservation command is generated.

## Roadmap

Unticked items are not built yet.

- [x] On-demand stockout detection from Cloud Logging: counts ZONE_RESOURCE_POOL_EXHAUSTED errors per zone and machine type, and reads the alternative zones Google suggests in the error (from exported log entries)
- [x] Spot VM check: obtainability score and estimated uptime per zone and machine type, read from saved `advice.capacity` (beta) responses
- [x] GPU, TPU and H4D check: recommended zone and time window, read from saved `advice.calendarMode` responses
- [x] Stockout report: which machine types and zones are at risk, with ranked zones to move to (`plan`)
- [x] Reservation step printed as a dry-run command only
- [ ] Live calls to `advice.capacity`, `advice.calendarMode` and Cloud Logging, so nothing has to be exported by hand
- [ ] Reservation plan: compares running VMs and autoscaler maximums with existing reservations and lists the gaps
- [ ] Reservation automation from a YAML policy: creates on-demand reservations and calendar-mode future reservation requests, dry-run by default, real purchases behind an explicit flag and a spend limit you set
- [ ] Flags unused and DEGRADED reservations so you stop paying for capacity you cannot use
- [ ] Slack or webhook alerts when a score drops below your threshold
- [ ] Scheduled run as a Cloud Run job or cron

Nothing in the current release can create, change or buy any cloud resource.

## Development

```bash
git clone https://github.com/narayana006/gcp-stockout-guard
cd gcp-stockout-guard
python -m venv .venv && source .venv/bin/activate
pip install -e . pytest ruff
ruff check .
pytest
```

Tests use fixture files only; they need no credentials and no network.

## Contributing

Issues and pull requests are welcome. Real-world stockout log entries and advice responses (with project names and instance names redacted) are especially useful, because they make the parsers more robust.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
