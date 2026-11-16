"""Read-only live calls to Cloud Logging and Compute Engine's capacity advice.

Only three endpoints are called, and none creates, changes or buys anything:

* ``POST https://logging.googleapis.com/v2/entries:list`` to read stockout
  errors from a project's logs.
* ``POST https://compute.googleapis.com/compute/beta/projects/P/regions/R/advice/capacity``
  to ask for Spot VM capacity advice (beta).
* ``POST https://compute.googleapis.com/compute/v1/projects/P/regions/R/advice/calendarMode``
  to ask where and when a future reservation is likely to be approved.

The access token comes from ``GOOGLE_OAUTH_ACCESS_TOKEN`` if it is set,
otherwise from ``gcloud auth print-access-token``. The HTTP transport and token
source are injectable so tests run without credentials or network.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .advice import short_zone

LOGGING_URL = "https://logging.googleapis.com/v2/entries:list"
CAPACITY_URL = (
    "https://compute.googleapis.com/compute/beta/projects/{project}/regions/{region}"
    "/advice/capacity"
)
CALENDAR_URL = (
    "https://compute.googleapis.com/compute/v1/projects/{project}/regions/{region}"
    "/advice/calendarMode"
)
TOKEN_ENV = "GOOGLE_OAUTH_ACCESS_TOKEN"
PAGE_SIZE = 1000

# (url, JSON body, headers) -> parsed JSON response
Transport = Callable[[str, dict[str, Any], dict[str, str]], Any]
TokenSource = Callable[[], str]


class LiveError(Exception):
    """A live call failed; the message is shown to the user."""


def default_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LiveError(
            f"cannot run gcloud to get an access token ({exc}); set {TOKEN_ENV}"
        ) from exc
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        detail = result.stderr.strip().splitlines()[-1:] or ["no token printed"]
        raise LiveError(f"gcloud auth print-access-token failed: {detail[0]}")
    return token


def urllib_transport(url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LiveError(f"{url} returned HTTP {exc.code}: {_error_message(exc)}") from exc
    except urllib.error.URLError as exc:
        raise LiveError(f"cannot reach {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise LiveError(f"{url} did not return JSON: {exc.msg}") from exc


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read().decode("utf-8"))
        return str(data["error"]["message"])
    except Exception:  # noqa: BLE001 - any unreadable body falls back to the reason
        return str(exc.reason)


class LiveClient:
    def __init__(
        self, transport: Transport | None = None, token: TokenSource | None = None
    ) -> None:
        self._transport = transport or urllib_transport
        self._token_source = token or default_token
        self._token: str | None = None

    def _post(self, url: str, body: dict[str, Any]) -> Any:
        if self._token is None:
            self._token = self._token_source()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        data = self._transport(url, body, headers)
        if not isinstance(data, dict):
            raise LiveError(f"{url} returned unexpected JSON (not an object)")
        return data

    def fetch_stockout_logs(
        self,
        project: str,
        days: float = 7,
        max_entries: int = 5000,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read stockout log entries from the last ``days`` days, newest first."""
        if days <= 0:
            raise ValueError("days must be greater than 0")
        if max_entries < 1:
            raise ValueError("max entries must be at least 1")
        body: dict[str, Any] = {
            "resourceNames": [f"projects/{project}"],
            "filter": stockout_filter(days, now),
            "orderBy": "timestamp desc",
            "pageSize": min(PAGE_SIZE, max_entries),
        }
        entries: list[dict[str, Any]] = []
        while True:
            data = self._post(LOGGING_URL, body)
            entries.extend(e for e in data.get("entries") or [] if isinstance(e, dict))
            token = data.get("nextPageToken")
            if len(entries) >= max_entries or not token:
                return entries[:max_entries]
            body = {**body, "pageToken": token}

    def fetch_capacity_advice(
        self, project: str, region: str, request_body: dict[str, Any]
    ) -> dict[str, Any]:
        """Send an advice.capacity request body and return the response."""
        url = CAPACITY_URL.format(project=project, region=region)
        return self._post(url, request_body)

    def fetch_calendar_advice(
        self, project: str, region: str, request_body: dict[str, Any]
    ) -> dict[str, Any]:
        """Send an advice.calendarMode request body and return the response."""
        url = CALENDAR_URL.format(project=project, region=region)
        return self._post(url, request_body)


def stockout_filter(days: float, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '("ZONE_RESOURCE_POOL_EXHAUSTED" OR '
        '"does not have enough resources available to fulfill the request") '
        f'AND timestamp>="{since}"'
    )


def region_of(zones: list[str]) -> str | None:
    """``us-central1-a`` -> ``us-central1``; None if the zones span several regions."""
    regions = {z.rsplit("-", 1)[0] for z in (short_zone(z) for z in zones) if z and "-" in z}
    return regions.pop() if len(regions) == 1 else None
