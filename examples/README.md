# Example inputs

Everything in this directory is invented. Project names, instance names and scores are fictional.

| File | What it is |
| --- | --- |
| `stockout_logs.json` | Log entries in the shape `gcloud logging read --format=json` prints, modelled on Compute Engine audit logs. |
| `advice_capacity_us-central1.json` | **Illustrative** `advice.capacity` responses (one per zone queried), written by hand from the field names in Google's public REST reference. Not recorded from a real project. |
| `advice_calendar_europe-west4.json` | **Illustrative** `advice.calendarMode` response, written by hand the same way. The `status` value and `details` text are examples only. |

If a real response of yours is parsed differently from these, please open an issue with a redacted copy.
