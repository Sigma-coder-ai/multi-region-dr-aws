# Drill report: `health-blip`, **REAL-AWS**

> ✅ **REAL-AWS RESULT.** Measured against deployed AWS resources.

- **Drill ID:** `aws-health-blip-b52fbb` (run `7d867a`)
- **Scenario:** Home health check fails for ~75 s (one controller tick). Must not fail over.
- **Failed region:** `ap-south-1`, **target:** `ap-southeast-1`
- **Generated:** 2026-10-18 18:00:07 on Obsidian
- **Result:** ✅ PASSED

## Checks

| Check | Result | Observed |
|---|---|---|
| no failover happened | ✅ | `None` |
| home region still active | ✅ | `ap-south-1` |

## Timeline (seconds after fault injection)

| Event | t + s |
|---|---|
| t1_first_failed_probe | 39.9 s |
| t2_decision | — |
| t3_promoted | — |
| alert | — |

## Recovery metrics

| Metric | Value |
|---|---|
| Detection (fault → first failed probe) | 39.855 s |
| Decision (first failed probe → failover decision) | — |
| Promotion (decision → control object promoted) | — |
| Read RTO (fault → first read served by target) | — |
| Longest user-visible read outage | 0.0 s |
| Write RTO (fault → first write acked by target) | — |
| Acknowledged writes lost at promotion | 0 |
| RPO (data-loss window) | 0.0 s |
| User read latency p50 during incident | 149.5 ms |
| User read latency p95 during incident | 180.1 ms |
| Lost writes that arrived late after the region recovered | 0 |

## Settings used

```json
{
  "controller_tick_s": 60,
  "probe_timeout_s": 3.0,
  "scorer": {
    "window": 10,
    "failover_threshold": 40,
    "consecutive_bad": 3,
    "fast_path_hard_failures": 2,
    "latency_good_ms": 800,
    "latency_bad_ms": 3000
  },
  "workflow": {
    "drain_max_s": 30.0,
    "replication_ok_pending": 0,
    "cooldown_s": 300.0,
    "verify_timeout_s": 120.0,
    "failback_min_healthy_s": 120.0,
    "lock_ttl_s": 600.0,
    "alert_repeat_s": 1800.0
  },
  "write_interval_s": 1.0,
  "read_interval_s": 2.0
}
```
