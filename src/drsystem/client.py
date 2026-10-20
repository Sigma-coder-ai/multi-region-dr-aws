"""Client SDK with client-side write failover.

Reads go through the edge (CloudFront locally simulated by the router) and fail over there.
Writes go to the *active* region named by ``/api/config``. On a timeout, 5xx or a fencing
rejection, the client refreshes the config and retries until a deadline. The same logic runs in
the browser dashboard (``web/app.js``) and in the RPO sequence writer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

RETRYABLE_ERRORS = {"READ_ONLY", "FROZEN", "STALE_EPOCH", "UNAVAILABLE", "DEMOTED", "OUTAGE", "INJECTED_ERROR"}


@dataclass
class WriteResult:
    ok: bool
    region: str | None
    attempts: int
    first_attempt_at: float
    acked_at: float | None
    body: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class DRClient:
    def __init__(self, base_url: str, timeout_s: float = 0.5) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout_s)
        self._active: str | None = None

    def close(self) -> None:
        self.http.close()

    # -- reads ----------------------------------------------------------------------------------
    def get(self, path: str) -> httpx.Response:
        return self.http.get(f"{self.base_url}/api/{path.lstrip('/')}")

    def _write_url(self, region: str, path: str) -> str:
        return f"{self.base_url}/r/{region}/{path.lstrip('/')}"

    def refresh_config(self) -> dict | None:
        try:
            r = self.get("config")
            if r.status_code == 200:
                cfg = r.json()
                self._active = cfg["active_region"]
                return cfg
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return None

    # -- writes ---------------------------------------------------------------------------------
    def write(self, path: str, payload: dict, headers: dict | None = None, deadline_s: float = 30.0) -> WriteResult:
        start = time.time()
        attempts = 0
        errors: list[str] = []
        backoff = 0.05
        while time.time() - start < deadline_s:
            if self._active is None and self.refresh_config() is None:
                errors.append("config_unavailable")
                time.sleep(backoff)
                backoff = min(backoff * 2, 0.5)
                continue
            region = self._active
            attempts += 1
            try:
                r = self.http.post(self._write_url(region, path), json=payload, headers=headers)
            except httpx.HTTPError as exc:
                errors.append(f"{region}:{type(exc).__name__}")
                self._active = None
                continue
            if r.status_code < 300:
                return WriteResult(True, region, attempts, start, time.time(), r.json(), errors)
            try:
                body = r.json()
            except ValueError:
                body = {}
            code = body.get("error", f"HTTP{r.status_code}")
            errors.append(f"{region}:{code}")
            if code not in RETRYABLE_ERRORS and r.status_code < 500 and r.status_code != 429:  # 429 = Lambda throttle
                return WriteResult(False, region, attempts, start, None, body, errors)
            hinted = body.get("active_region")
            self._active = hinted if hinted and hinted != region else None
            time.sleep(backoff)
            backoff = min(backoff * 2, 0.5)
        return WriteResult(False, None, attempts, start, None, {}, errors)
