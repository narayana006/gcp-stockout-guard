# gcp-stockout-guard

Spot Google Cloud capacity stockouts early and automate Compute Engine reservations, using the Compute Engine advice APIs and stockout error logs.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A command-line tool and scheduled job.

**The problem it solves:** When a Google Cloud zone runs out of a machine type, VM creation and autoscaling fail with ZONE_RESOURCE_POOL_EXHAUSTED, and teams usually find out during a scale-up or an incident. Reserving capacity ahead of time prevents it, but working out what to reserve, where and when is manual work.

## Who it is for

SRE and platform teams running workloads on Google Compute Engine or GKE who have been hit by capacity stockouts, or who must guarantee capacity for a launch, a peak or GPU work.

## How it will work

1. You tell the tool which projects, regions and machine types matter to you.
2. It asks Google's advice APIs how obtainable that capacity is, reads your logs for stockout errors and compares your usage with your reservations.
3. It reports where you are at risk and what to reserve; if you allow it, it creates the reservations within the limits you set.

## What Google's APIs can and cannot tell you

- **Spot VMs:** `advice.capacity` returns an obtainability score from 0.0 to 1.0 (0.7 and above is a high chance of success) and an estimated uptime. It covers Spot VMs only and is in beta.
- **GPUs, TPUs and H4D:** `advice.calendarMode` says which zones have capacity for a future time window, so a calendar-mode reservation request is likely to be approved.
- **Standard on-demand VMs:** Google publishes no forward-looking availability score. This tool can only detect these stockouts after they start, from error logs, and reduce the risk with reservations.
- **No guarantee:** a good score is a likelihood, not a promise. Capacity can change between the check and the request.

## Safety

Reservations are billed whether or not you use them. The tool never creates one unless you pass an explicit flag, and it refuses to exceed the spend limit in your policy. It needs read access for checks and reservation-admin access only for the `reserve` command.

## Planned features

Unticked items are not built yet.

- [ ] Spot VM check: obtainability score and estimated uptime per region and zone from the `advice.capacity` API (beta)
- [ ] GPU, TPU and H4D check: future availability per zone and time window from the `advice.calendarMode` API
- [ ] On-demand stockout detection from Cloud Logging: counts ZONE_RESOURCE_POOL_EXHAUSTED errors per zone and machine type, and reads the alternative zones Google suggests in the error
- [ ] Stockout report: which machine types and zones are at risk, with recommended zones to move to
- [ ] Reservation plan: compares running VMs and autoscaler maximums with existing reservations and lists the gaps
- [ ] Reservation automation from a YAML policy: creates on-demand reservations and calendar-mode future reservation requests
- [ ] Dry-run by default; real purchases need an explicit flag and stay under a spend limit you set
- [ ] Flags unused and DEGRADED reservations so you stop paying for capacity you cannot use
- [ ] Slack or webhook alerts when a score drops below your threshold
- [ ] Scheduled run as a Cloud Run job or cron

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
# check Spot obtainability for 50 VMs across a region
gcp-stockout-guard check --project my-project --region us-central1 --machine-type n2-standard-8 --count 50 --spot

# show stockout errors from the last 7 days
gcp-stockout-guard stockouts --project my-project --days 7

# show what the policy would reserve, without buying anything
gcp-stockout-guard reserve --policy reservations.yaml --dry-run
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## License

[MIT](LICENSE)
