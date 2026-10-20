# Drill report: `gray-errors`, **REAL-AWS**

> ✅ **REAL-AWS RESULT.** Measured against deployed AWS resources.

- **Drill ID:** `aws-gray-errors-ac0824` (run `7d867a`)
- **Scenario:** Home app answers 70% of requests with HTTP 500.
- **Failed region:** `ap-south-1`, **target:** `ap-southeast-1`
- **Generated:** 2026-10-18 17:53:27 on Obsidian
- **Result:** ✅ PASSED

## Checks

| Check | Result | Observed |
|---|---|---|
| failover workflow completed | ✅ | `failed_over` |
| DR region holds write authority | ✅ | `ap-southeast-1` |
| writes resumed in DR | ✅ | `253.543` |
| failback completed | ✅ | `failed_back` |
| failback lost 0 writes (planned RPO = 0) | ✅ | `0` |

## Timeline (seconds after fault injection)

| Event | t + s |
|---|---|
| t1_first_failed_probe | 51.0 s |
| t2_decision | 230.9 s |
| t3_promoted | 252.0 s |
| alert | — |

## Recovery metrics

| Metric | Value |
|---|---|
| Detection (fault → first failed probe) | 50.967 s |
| Decision (first failed probe → failover decision) | 179.896 s |
| Promotion (decision → control object promoted) | 21.186 s |
| Read RTO (fault → first read served by target) | 0.723 s |
| Longest user-visible read outage | 0.0 s |
| Write RTO (fault → first write acked by target) | 253.543 s |
| Acknowledged writes lost at promotion | 0 |
| RPO (data-loss window) | 0.0 s |
| User read latency p50 during incident | 210.9 ms |
| User read latency p95 during incident | 623.4 ms |
| Lost writes that arrived late after the region recovered | 0 |

## Failover workflow steps

| Step | Duration | Details |
|---|---|---|
| reassess | 6.358 s | `{"source_failures": 2, "standby_ready": true, "confirmed": true}` |
| drain | 14.349 s | `{"source": "ap-south-1", "pending": 0, "oldest_pending_age_s": 0.0, "drained": true, "pending_keys_sample": []}` |
| promote | 0.135 s | `{"promoted": true, "epoch": 14, "demoted": "ap-south-1"}` |
| notify | 0.115 s | `{"notified": true}` |
| verify | 1.058 s | `{"reads_ok": true, "writes_ok": true, "target": "ap-southeast-1"}` |

## Controlled failback

- **Outcome:** `failed_back`, total 544.5 s, writes frozen for 419.9 s
- **Writes acknowledged during failback:** 137, **lost:** 0

| Step | Duration |
|---|---|
| preconditions | 124.086 s |
| freeze_writes | 0.594 s |
| drain_to_home | 18.022 s |
| verify_data | 399.483 s |
| await_control_sync | 0.212 s |
| promote_home | 0.467 s |
| verify_traffic | 1.643 s |

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
