import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gcp_stockout_guard import cli, live
from gcp_stockout_guard.live import LiveClient, LiveError, region_of, stockout_filter
from gcp_stockout_guard.stockouts import find_stockouts, load_entries, summarize

EXAMPLES = Path(__file__).parent.parent / "examples"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    """Returns canned responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.responses.pop(0)


def make_client(*responses):
    transport = FakeTransport(*responses)
    tokens = []

    def token():
        tokens.append(1)
        return "test-token"

    return LiveClient(transport=transport, token=token), transport, tokens


def test_filter_matches_both_markers_and_time_window():
    text = stockout_filter(7, NOW)
    assert '"ZONE_RESOURCE_POOL_EXHAUSTED"' in text
    assert "does not have enough resources available" in text
    assert 'timestamp>="2026-11-08T12:00:00Z"' in text


def test_fetch_logs_follows_pages_and_reuses_token():
    entries = load_entries((EXAMPLES / "stockout_logs.json").read_text(encoding="utf-8"))
    client, transport, tokens = make_client(
        {"entries": entries[:4], "nextPageToken": "p2"},
        {"entries": entries[4:]},
    )
    got = client.fetch_stockout_logs("example-project", days=7, now=NOW)
    assert got == entries
    assert len(transport.calls) == 2 and len(tokens) == 1
    url, body, headers = transport.calls[0]
    assert url == live.LOGGING_URL
    assert body["resourceNames"] == ["projects/example-project"]
    assert body["orderBy"] == "timestamp desc"
    assert "pageToken" not in body
    assert transport.calls[1][1]["pageToken"] == "p2"
    assert headers["Authorization"] == "Bearer test-token"
    # The fetched entries feed the existing parser unchanged.
    assert summarize(find_stockouts(got))[0].zone == "us-central1-a"


def test_fetch_logs_stops_at_max_entries():
    page = {"entries": [{"n": i} for i in range(3)], "nextPageToken": "more"}
    client, transport, _ = make_client(page, page)
    got = client.fetch_stockout_logs("p", max_entries=2, now=NOW)
    assert len(got) == 2 and len(transport.calls) == 1
    assert transport.calls[0][1]["pageSize"] == 2


def test_fetch_logs_handles_empty_response():
    client, _, _ = make_client({})
    assert client.fetch_stockout_logs("p", now=NOW) == []


def test_fetch_logs_rejects_bad_arguments():
    client, transport, _ = make_client()
    with pytest.raises(ValueError):
        client.fetch_stockout_logs("p", days=0)
    with pytest.raises(ValueError):
        client.fetch_stockout_logs("p", max_entries=0)
    assert transport.calls == []


def test_non_object_response_is_an_error():
    client, _, _ = make_client([1, 2])
    with pytest.raises(LiveError, match="not an object"):
        client.fetch_stockout_logs("p", now=NOW)


def test_region_of():
    assert region_of(["us-central1-a", "zones/us-central1-c"]) == "us-central1"
    assert region_of(["us-central1-a", "europe-west4-b"]) is None
    assert region_of([]) is None


def test_default_token_prefers_environment(monkeypatch):
    monkeypatch.setenv(live.TOKEN_ENV, " env-token ")
    assert live.default_token() == "env-token"


def test_default_token_reports_gcloud_failure(monkeypatch):
    monkeypatch.delenv(live.TOKEN_ENV, raising=False)

    class Result:
        returncode = 1
        stdout = ""
        stderr = "ERROR: (gcloud.auth) You do not currently have an active account."

    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(LiveError, match="active account"):
        live.default_token()


def test_http_error_message_is_readable(monkeypatch):
    def fail(request, timeout):
        body = io.BytesIO(json.dumps({"error": {"message": "Permission denied"}}).encode())
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, body)

    monkeypatch.setattr(live.urllib.request, "urlopen", fail)
    with pytest.raises(LiveError, match="HTTP 403: Permission denied"):
        live.urllib_transport("https://example.invalid/x", {}, {})


def run_cli(argv, client):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdin=io.StringIO(), stdout=out, stderr=err, client=client)
    return code, out.getvalue(), err.getvalue()


def test_cli_fetch_logs_output_feeds_stockouts():
    entries = load_entries((EXAMPLES / "stockout_logs.json").read_text(encoding="utf-8"))
    client, _, _ = make_client({"entries": entries})
    code, out, _ = run_cli(["fetch-logs", "--project", "example-project"], client)
    assert code == 0
    out2 = io.StringIO()
    assert cli.main(["stockouts", "-"], stdin=io.StringIO(out), stdout=out2) == 0
    assert "us-central1-a" in out2.getvalue()


def test_cli_fetch_capacity_derives_region_and_sends_body():
    saved = (EXAMPLES / "advice_capacity_us-central1.json").read_text(encoding="utf-8")
    response = json.loads(saved)[0]
    client, transport, _ = make_client(response)
    argv = ["fetch-capacity", "--project", "example-project", "--machine-type",
            "n2-standard-8", "--count", "20", "--zone", "us-central1-c"]
    code, out, _ = run_cli(argv, client)
    assert code == 0
    assert json.loads(out) == response
    url, body, _ = transport.calls[0]
    assert url.endswith("/projects/example-project/regions/us-central1/advice/capacity")
    assert body["size"] == 20
    assert body["distributionPolicy"]["zones"] == [{"zone": "zones/us-central1-c"}]


def test_cli_fetch_capacity_needs_a_region():
    client, transport, _ = make_client()
    argv = ["fetch-capacity", "--project", "p", "--machine-type", "n2-standard-8",
            "--count", "1"]
    code, _, err = run_cli(argv, client)
    assert code == 2 and "--region" in err and transport.calls == []


def test_cli_reports_live_errors():
    def broken(url, body, headers):
        raise LiveError("cannot reach logging.googleapis.com: offline")

    code, _, err = run_cli(
        ["fetch-logs", "--project", "p"], LiveClient(transport=broken, token=lambda: "t")
    )
    assert code == 2 and "offline" in err
