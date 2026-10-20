"""Independent health prober (runs in the DR region, probes both regions' /health)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import httpx


@dataclass(frozen=True)
class ProbeResult:
    region: str
    at: float
    ok: bool  # HTTP 200 and ready
    status: int | None
    latency_ms: float
    hard_failure: bool  # timeout / connection error (region unreachable)
    ready: bool = False
    active: bool = False
    epoch: int | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProbeResult":
        return cls(**d)


class HttpProber:
    def __init__(self, endpoints: dict[str, str], timeout_s: float, auth: httpx.Auth | None = None) -> None:
        self.endpoints = endpoints
        self.http = httpx.Client(timeout=timeout_s, auth=auth)

    def probe(self, region: str) -> ProbeResult:
        url = self.endpoints[region].rstrip("/") + "/health"
        started = time.time()
        t0 = time.perf_counter()
        try:
            r = self.http.get(url)
        except httpx.HTTPError as exc:
            return ProbeResult(region, started, False, None, (time.perf_counter() - t0) * 1000, True, error=type(exc).__name__)
        latency = (time.perf_counter() - t0) * 1000
        try:
            body = r.json()
        except ValueError:
            body = {}
        return ProbeResult(
            region=region,
            at=started,
            ok=r.status_code == 200 and bool(body.get("ready")),
            status=r.status_code,
            latency_ms=latency,
            hard_failure=False,
            ready=bool(body.get("ready")),
            active=bool(body.get("active")),
            epoch=body.get("epoch"),
            error="" if r.status_code == 200 else str(body.get("error", r.status_code)),
        )

    def probe_many(self, region: str, n: int, gap_s: float = 0.2, sleep=time.sleep) -> list[ProbeResult]:
        """Take up to ``n`` samples. Stops early after 2 unreachable samples (no point waiting out
        more timeouts when the region does not answer at all)."""
        out: list[ProbeResult] = []
        for i in range(max(1, n)):
            p = self.probe(region)
            out.append(p)
            if len(out) >= 2 and out[-1].hard_failure and out[-2].hard_failure:
                break
            if i < n - 1:
                sleep(gap_s)
        return out

    def close(self) -> None:
        self.http.close()
