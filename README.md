# gcp-stockout-guard

Find out which Google Cloud zones and machine types are running out of capacity, from the stockout errors already sitting in your Compute Engine logs.

> **Status: early.** One command works today: `stockouts`, which counts `ZONE_RESOURCE_POOL_EXHAUSTED` errors per zone and machine type from exported log entries and lists the alternative zones Google suggested. Forward-looking capacity checks and reservation automation are **not built yet**; see [Roadmap](#roadmap).

## Quick start

Requires Python 3.10 or newer. There are no runtime dependencies.

```bash
pip install git+https://github.com/narayana006/gcp-stockout-guard
```

Export the last 7 days of stockout errors with `gcloud` and pipe them in:

```bash
gcloud logging read '"ZONE_RESOURCE_POOL_EXHAUSTED"' --project my-project --freshness=7d --format=json | gcp-stockout-guard stockouts
```

No Google Cloud project to hand? Clone the repository and run it on the bundled sample:

```bash
gcp-stockout-guard stockouts tests/fixtures/audit_log.json
```

```
4 stockout errors across 2 zone and machine type pairs (6 log entries read)

ZONE            MACHINE TYPE   ERRORS  FIRST SEEN (UTC)  LAST SEEN (UTC)   GOOGLE SUGGESTS
us-central1-a   n2-standard-8  3       2026-11-07 09:30  2026-11-08 15:00  us-central1-c, us-central1-f
europe-west4-b  a2-highgpu-1g  1       2026-11-06 22:15  2026-11-06 22:15  -
```

## The problem

When a Google Cloud zone runs out of a machine type, VM creation and autoscaling fail with `ZONE_RESOURCE_POOL_EXHAUSTED`. Teams usually find out during a scale-up or an incident. The errors are in Cloud Logging, but nobody reads them until something breaks, and the zones Google suggests as alternatives are buried inside each error.

This tool is for SRE and platform teams running workloads on Compute Engine or GKE who have been hit by stockouts, or who must guarantee capacity for a launch, a peak or GPU work.

## The `stockouts` command

```
gcp-stockout-guard stockouts [FILE] [--format text|json] [--fail-on-stockout]
```

| Argument | Meaning |
| --- | --- |
| `FILE` | Log entries as JSON. Use `-` or leave it out to read standard input. |
| `--format json` | Print machine-readable JSON instead of the table. |
| `--fail-on-stockout` | Exit with status 1 if any stockout error is found, for use in cron or CI. |

Exit status is 0 on success, 1 when `--fail-on-stockout` is set and stockouts were found, and 2 when the input cannot be read or is not a list of log entries.

**Input.** The output of `gcloud logging read --format=json` (a JSON array of log entries), or one JSON entry per line. The tool does not call the Cloud Logging API itself; you export, it analyses. That means it needs no credentials and makes no network calls.

**What counts as a stockout.** Any entry containing `ZONE_RESOURCE_POOL_EXHAUSTED` (including `ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`) or the message "does not have enough resources available to fulfill the request". Other errors, such as `QUOTA_EXCEEDED`, are ignored.

**Where each column comes from.**

- *Zone and machine type*: the error's structured details (`zone`, `vmType`) when present; otherwise the entry's resource labels, resource paths such as `zones/us-central1-a` and `machineTypes/n2-standard-8`, or the error message. If the entry names neither, the value is `unknown`.
- *Google suggests*: the `zonesAvailable` list in the error's structured details, most frequently suggested zone first. A `-` means the errors carried no suggestions.

JSON output has the same data:

```json
{
  "entries_read": 6,
  "stockout_errors": 4,
  "groups": [
    {
      "zone": "us-central1-a",
      "machine_type": "n2-standard-8",
      "errors": 3,
      "first_seen": "2026-11-07T09:30:00+00:00",
      "last_seen": "2026-11-08T15:00:00+00:00",
      "suggested_zones": ["us-central1-c", "us-central1-f"]
    }
  ]
}
```

## Limitations

- This is detection after the fact. It tells you where stockouts have already happened, not where they will.
- The parser is tested against sample entries modelled on Compute Engine audit logs, not against a live project. Log shapes vary by API version and by how the VM was created (direct insert, managed instance group, GKE). If an entry of yours shows up as `unknown`, please open an issue with a redacted copy.
- A suggested zone is Google's hint at the time of the error. Capacity can change before you act on it.

## Roadmap

Unticked items are not built yet.

- [ ] Spot VM check: obtainability score and estimated uptime per region and zone from the `advice.capacity` API (beta)
- [ ] GPU, TPU and H4D check: future availability per zone and time window from the `advice.calendarMode` API
- [x] On-demand stockout detection from Cloud Logging: counts ZONE_RESOURCE_POOL_EXHAUSTED errors per zone and machine type, and reads the alternative zones Google suggests in the error (from exported log entries)
- [ ] Stockout report: which machine types and zones are at risk, with recommended zones to move to
- [ ] Reservation plan: compares running VMs and autoscaler maximums with existing reservations and lists the gaps
- [ ] Reservation automation from a YAML policy: creates on-demand reservations and calendar-mode future reservation requests
- [ ] Dry-run by default; real purchases need an explicit flag and stay under a spend limit you set
- [ ] Flags unused and DEGRADED reservations so you stop paying for capacity you cannot use
- [ ] Slack or webhook alerts when a score drops below your threshold
- [ ] Scheduled run as a Cloud Run job or cron

### What Google's APIs can and cannot tell you

This shapes the roadmap, so it is worth stating up front:

- **Spot VMs:** `advice.capacity` returns an obtainability score from 0.0 to 1.0 and an estimated uptime. It covers Spot VMs only and is in beta.
- **GPUs, TPUs and H4D:** `advice.calendarMode` says which zones have capacity for a future time window, so a calendar-mode reservation request is likely to be approved.
- **Standard on-demand VMs:** Google publishes no forward-looking availability score. Stockouts can only be detected after they start, from error logs (what this tool does today), and reduced with reservations.

### Safety, when reservations arrive

Reservations are billed whether or not you use them. The planned `reserve` command will be dry-run by default, will need an explicit flag to buy anything and will refuse to exceed a spend limit you set. Nothing in the current release can create, change or buy any cloud resource.

## Development

```bash
git clone https://github.com/narayana006/gcp-stockout-guard
cd gcp-stockout-guard
python -m venv .venv && source .venv/bin/activate
pip install -e . pytest ruff
ruff check .
pytest
```

Tests use fixture log entries only; they need no credentials and no network.

## Contributing

Issues and pull requests are welcome. Real-world stockout log entries (with project names and instance names redacted) are especially useful, because they make the parser more robust.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
