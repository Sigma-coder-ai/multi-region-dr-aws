"""Client for the real deployment: talks to the regional function URLs directly (no edge yet).

Reads use client-side origin failover with CloudFront's rules: try the home region first, and on a
connection error, timeout or HTTP 500/502/503/504, retry the same GET on the other region.
Writes reuse ``DRClient`` logic: go to the active region from ``/config``, refresh and retry.
"""

from __future__ import annotations

import httpx

from ..client import DRClient

FAILOVER_STATUSES = {500, 502, 503, 504}


class AwsDirectClient(DRClient):
    def __init__(self, endpoints: dict[str, str], home: str, auth: httpx.Auth | None, timeout_s: float = 5.0) -> None:
        self.endpoints = {r: u.rstrip("/") for r, u in endpoints.items()}
        self.order = [home] + [r for r in endpoints if r != home]
        self.http = httpx.Client(timeout=timeout_s, auth=auth)
        self._active = None
        self.base_url = ""

    def get(self, path: str) -> httpx.Response:
        last_resp: httpx.Response | None = None
        last_exc: Exception | None = None
        for region in self.order:
            try:
                resp = self.http.get(f"{self.endpoints[region]}/{path.lstrip('/')}")
            except httpx.HTTPError as exc:
                last_exc = exc
                continue
            if resp.status_code not in FAILOVER_STATUSES:
                return resp
            last_resp = resp
        if last_resp is not None:
            return last_resp
        raise last_exc or httpx.ConnectError("no regions configured")

    def _write_url(self, region: str, path: str) -> str:
        return f"{self.endpoints[region]}/{path.lstrip('/')}"
