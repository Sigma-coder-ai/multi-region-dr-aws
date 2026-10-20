"""Recovery-time optimizer: find the cheapest AWS configuration that meets RTO/RPO targets.

This is a **MODEL ESTIMATE** for the planned AWS deployment (Option A), not a measurement. It
enumerates real AWS knobs, estimates each RTO component, and estimates monthly cost against the
Always Free allowances. Once REAL-AWS drills exist, their measured component medians replace the
assumed overheads (``Overheads``), so the model gets calibrated by evidence.

AWS constraints encoded here
- EventBridge Scheduler's minimum rate is 1 minute; faster probing needs a looping Lambda.
- CloudFront origin failover: connection timeout 1-10 s, connection attempts 1-3 (per request).
- Lambda Always Free: 1M requests + 400,000 GB-s per month; arm64 $0.0000133334/GB-s beyond it.
- Step Functions Standard Always Free: 4,000 state transitions per month.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass

SECONDS_PER_MONTH = 30 * 24 * 3600
LAMBDA_FREE_GB_S = 400_000
LAMBDA_FREE_REQUESTS = 1_000_000
LAMBDA_ARM_PER_GB_S = 0.0000133334
LAMBDA_PER_MILLION_REQ = 0.20


@dataclass(frozen=True)
class Overheads:
    """Assumed per-step overheads (seconds). Replace with REAL-AWS medians when available."""

    probe_timeout_s: float = 3.0
    workflow_start_s: float = 1.0  # EventBridge/Step Functions start latency
    reassess_s: float = 4.0
    promote_s: float = 1.0  # conditional PutObject in the DR bucket
    client_refresh_s: float = 2.0  # client notices READ_ONLY/timeout and re-reads /config
    base_lambda_gb_s: float = 10_000  # app + scorer + workflows at lab traffic
    base_lambda_requests: float = 150_000
    source: str = "ASSUMED"


@dataclass(frozen=True)
class Candidate:
    probe_interval_s: int
    fast_path_failures: int
    drain_max_s: int
    cf_connection_timeout_s: int
    cf_connection_attempts: int


@dataclass(frozen=True)
class Estimate:
    candidate: Candidate
    detection_s: float
    write_rto_s: float
    read_outage_s: float
    worst_case_rpo_s: float  # hard regional outage: unreplicated data is stranded, draining cannot help
    gray_failure_rpo_s: float  # source still reachable: drain wait lets replication catch up
    monthly_cost_usd: float
    lambda_gb_s: float
    meets_targets: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(d.pop("candidate"))
        return d


def estimate(c: Candidate, ov: Overheads, replication_lag_s: float, target_rto_s: float, target_rpo_s: float) -> Estimate:
    detection = c.probe_interval_s * c.fast_path_failures + ov.probe_timeout_s
    write_rto = detection + ov.workflow_start_s + ov.reassess_s + c.drain_max_s + ov.promote_s + ov.client_refresh_s
    read_outage = c.cf_connection_timeout_s * c.cf_connection_attempts  # added latency per read; reads keep succeeding
    worst_rpo = replication_lag_s
    gray_rpo = max(0.0, replication_lag_s - c.drain_max_s)

    # Probing cost: 1/min schedule = one short invocation; sub-minute = a Lambda looping for 60 s each minute.
    if c.probe_interval_s >= 60:
        probe_gb_s = (SECONDS_PER_MONTH / c.probe_interval_s) * 0.5 * 0.128
        probe_requests = SECONDS_PER_MONTH / c.probe_interval_s
    else:
        probe_gb_s = (SECONDS_PER_MONTH / 60) * 60 * 0.128
        probe_requests = SECONDS_PER_MONTH / 60
    gb_s = ov.base_lambda_gb_s + probe_gb_s
    req = ov.base_lambda_requests + probe_requests
    cost = max(0.0, gb_s - LAMBDA_FREE_GB_S) * LAMBDA_ARM_PER_GB_S + max(0.0, req - LAMBDA_FREE_REQUESTS) / 1e6 * LAMBDA_PER_MILLION_REQ
    meets = write_rto <= target_rto_s and worst_rpo <= target_rpo_s
    return Estimate(
        c, round(detection, 1), round(write_rto, 1), float(read_outage), round(worst_rpo, 1), round(gray_rpo, 1),
        round(cost, 2), round(gb_s), meets,
    )


def optimize(
    target_rto_s: float = 300,
    target_rpo_s: float = 60,
    replication_lag_s: float = 15.0,
    overheads: Overheads | None = None,
) -> list[Estimate]:
    ov = overheads or Overheads()
    grid = itertools.product([60, 30, 20, 10], [2, 3], [0, 5, 15, 30], [10, 5, 2, 1], [3, 1])
    out = [estimate(Candidate(*g), ov, replication_lag_s, target_rto_s, target_rpo_s) for g in grid]
    # Cheapest first, then the least Always Free allowance consumed (headroom protects credits),
    # then lowest RPO, then lowest write RTO, then most false-positive-resistant.
    out.sort(
        key=lambda e: (
            not e.meets_targets,
            e.monthly_cost_usd,
            e.lambda_gb_s,
            e.gray_failure_rpo_s,
            e.write_rto_s,
            e.read_outage_s,
            -e.candidate.fast_path_failures,
        )
    )
    return out


def optimizer_markdown(results: list[Estimate], target_rto_s: float, target_rpo_s: float, replication_lag_s: float, ov: Overheads) -> str:
    best = next((e for e in results if e.meets_targets), None)
    lines = [
        "# Recovery-time optimizer, **MODEL ESTIMATE**",
        "",
        "> ℹ️ Analytical model of the planned AWS deployment. Step overheads are "
        + ("**calibrated from REAL-AWS drills**. " if ov.source == "REAL-AWS" else "**assumed defaults** (replace with REAL-AWS medians once real drills exist). ")
        + "This is not a measurement.",
        "",
        f"- **Targets:** write RTO ≤ {target_rto_s} s, RPO ≤ {target_rpo_s} s",
        f"- **Assumed S3 replication lag:** {replication_lag_s} s (S3 CRR has no SLA without Replication Time Control)",
        "",
    ]
    if best:
        c = best.candidate
        lines += [
            "## Recommendation",
            "",
            f"- **Probe interval:** {c.probe_interval_s} s, fast path after {c.fast_path_failures} unreachable probes",
            f"- **Drain wait before promotion:** {c.drain_max_s} s",
            f"- **CloudFront origin failover:** connection timeout {c.cf_connection_timeout_s} s × {c.cf_connection_attempts} attempt(s)",
            f"- **Estimated:** write RTO **{best.write_rto_s} s**, RPO **{best.worst_case_rpo_s} s** (hard outage) / "
            f"**{best.gray_failure_rpo_s} s** (gray failure), "
            f"added read latency during outage ≤ {best.read_outage_s} s, cost **${best.monthly_cost_usd}/month** "
            f"({best.lambda_gb_s:,} Lambda GB-s of 400,000 free)",
            "",
        ]
    else:
        lines += ["## No configuration meets the targets. Relax the targets or reduce overheads.", ""]
    lines += [
        "## Top alternatives",
        "",
        "| Probe s | Fast path | Drain s | CF timeout × attempts | Detect s | Write RTO s | RPO hard / gray s | $/month | Meets |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for e in results[:12]:
        c = e.candidate
        lines.append(
            f"| {c.probe_interval_s} | {c.fast_path_failures} | {c.drain_max_s} | {c.cf_connection_timeout_s}×{c.cf_connection_attempts} | "
            f"{e.detection_s} | {e.write_rto_s} | {e.worst_case_rpo_s} / {e.gray_failure_rpo_s} | {e.monthly_cost_usd} | {'✅' if e.meets_targets else '❌'} |"
        )
    lines.append("")
    return "\n".join(lines)
