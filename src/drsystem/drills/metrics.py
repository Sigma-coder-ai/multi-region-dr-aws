"""Pure functions that turn raw drill observations into RTO/RPO numbers.

Definitions (all times are wall-clock seconds; t0 = fault injected)
  detection_s     t1 first failed probe            - t0
  decision_s      t2 failover decision             - t1
  promotion_s     t3 control object promoted       - t2
  read_outage_s   longest run of failed user reads after t0 (0 if the edge masked the failure)
  read_rto_s      first user read served by the target region - t0
  write_rto_s     first write acknowledged by the target region - t0
  lost_writes     writes acknowledged by the failed region that were missing in the target
                  region right after promotion (measured while the failed region is still down)
  rpo_s           how far back in time data was lost: max(t0, last source ack before promotion)
                  - earliest lost ack (0 when nothing was lost)
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class WriteRecord:
    seq: int
    first_attempt_at: float
    acked_at: float | None
    region: str | None
    ok: bool
    attempts: int


@dataclass(frozen=True)
class ReadSample:
    at: float
    ok: bool
    region: str | None
    latency_ms: float
    failover: bool = False


@dataclass(frozen=True)
class RecoveryMetrics:
    detection_s: float | None
    decision_s: float | None
    promotion_s: float | None
    read_outage_s: float
    read_rto_s: float | None
    write_rto_s: float | None
    lost_writes: int
    rpo_s: float
    writes_total: int
    writes_failed: int
    read_p50_ms: float | None
    read_p95_ms: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def _delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(a - b, 3)


def longest_failure_window(samples: list[ReadSample], since: float) -> float:
    longest = 0.0
    start: float | None = None
    for s in sorted(samples, key=lambda x: x.at):
        if s.at < since:
            continue
        if not s.ok and start is None:
            start = s.at
        elif s.ok and start is not None:
            longest = max(longest, s.at - start)
            start = None
    if start is not None:
        last = max(x.at for x in samples)
        longest = max(longest, last - start)
    return round(longest, 3)


def lost_writes(writes: list[WriteRecord], source: str, promoted_at: float, present_in_target: set[int]) -> list[WriteRecord]:
    return [
        w
        for w in writes
        if w.ok and w.region == source and w.acked_at is not None and w.acked_at < promoted_at and w.seq not in present_in_target
    ]


def rpo_window(lost: list[WriteRecord], writes: list[WriteRecord], source: str, promoted_at: float, t0: float) -> float:
    if not lost:
        return 0.0
    earliest = min(w.acked_at for w in lost if w.acked_at is not None)
    last_source = max(
        w.acked_at for w in writes if w.ok and w.region == source and w.acked_at is not None and w.acked_at < promoted_at
    )
    return round(max(0.0, max(t0, last_source) - earliest), 3)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1], 1)


def compute(
    t0: float,
    t1: float | None,
    t2: float | None,
    t3: float | None,
    source: str,
    target: str,
    writes: list[WriteRecord],
    reads: list[ReadSample],
    present_in_target: set[int],
) -> RecoveryMetrics:
    first_read_target = next((r.at for r in sorted(reads, key=lambda r: r.at) if r.at >= t0 and r.ok and r.region == target), None)
    first_write_target = min(
        (w.acked_at for w in writes if w.ok and w.region == target and w.acked_at is not None and w.acked_at >= t0),
        default=None,
    )
    lost = lost_writes(writes, source, t3, present_in_target) if t3 else []
    incident_reads = [r.latency_ms for r in reads if r.at >= t0 and r.ok]
    return RecoveryMetrics(
        detection_s=_delta(t1, t0),
        decision_s=_delta(t2, t1),
        promotion_s=_delta(t3, t2),
        read_outage_s=longest_failure_window(reads, t0),
        read_rto_s=_delta(first_read_target, t0),
        write_rto_s=_delta(first_write_target, t0),
        lost_writes=len(lost),
        rpo_s=rpo_window(lost, writes, source, t3, t0) if t3 else 0.0,
        writes_total=len(writes),
        writes_failed=sum(not w.ok for w in writes),
        read_p50_ms=percentile(incident_reads, 50),
        read_p95_ms=percentile(incident_reads, 95),
    )


def summarize(values: list[float | None]) -> dict:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "p90": None, "min": None, "max": None}
    return {
        "n": len(vals),
        "median": round(statistics.median(vals), 3),
        "p90": percentile(vals, 90) if len(vals) > 1 else round(vals[0], 3),
        "min": round(vals[0], 3),
        "max": round(vals[-1], 3),
    }
