# Drill report: `gray-errors`, **REAL-AWS**

> ✅ **REAL-AWS RESULT.** Measured against deployed AWS resources.

- **Drill ID:** `aws-gray-errors-65bea3` (run `7d867a`)
- **Scenario:** Home app answers 70% of requests with HTTP 500.
- **Failed region:** `ap-south-1`, **target:** `ap-southeast-1`
- **Generated:** 2026-10-18 14:19:54 on Obsidian
- **Result:** ✅ PASSED

## Checks

| Check | Result | Observed |
|---|---|---|
| failover workflow completed | ✅ | `failed_over` |
| DR region holds write authority | ✅ | `ap-southeast-1` |
| writes resumed in DR | ✅ | `415.375` |
| failback completed | ✅ | `failed_back` |
| failback lost 0 writes (planned RPO = 0) | ✅ | `0` |

## Timeline (seconds after fault injection)

| Event | t + s |
|---|---|
| t1_first_failed_probe | 90.3 s |
| t2_decision | 390.3 s |
| t3_promoted | 415.0 s |
| alert | — |

## Recovery metrics

| Metric | Value |
|---|---|
| Detection (fault → first failed probe) | 90.288 s |
| Decision (first failed probe → failover decision) | 300.0 s |
| Promotion (decision → control object promoted) | 24.663 s |
| Read RTO (fault → first read served by target) | 2.269 s |
| Longest user-visible read outage | 0.0 s |
| Write RTO (fault → first write acked by target) | 415.375 s |
| Acknowledged writes lost at promotion | 0 |
| RPO (data-loss window) | 0.0 s |
| User read latency p50 during incident | 176.0 ms |
| User read latency p95 during incident | 307.1 ms |
| Lost writes that arrived late after the region recovered | 0 |

## Failover workflow steps

| Step | Duration | Details |
|---|---|---|
| reassess | 6.356 s | `{"source_failures": 2, "standby_ready": true, "confirmed": true}` |
| drain | 17.981 s | `{"source": "ap-south-1", "pending": 0, "oldest_pending_age_s": 0.0, "drained": true, "pending_keys_sample": []}` |
| promote | 0.123 s | `{"promoted": true, "epoch": 5, "demoted": "ap-south-1"}` |
| notify | 0.054 s | `{"notified": true}` |
| verify | 1.536 s | `{"reads_ok": true, "writes_ok": true, "target": "ap-southeast-1"}` |

## Controlled failback

- **Outcome:** `failed_back`, total 214.2 s, writes frozen for 93.4 s
- **Writes acknowledged during failback:** 137, **lost:** 0

| Step | Duration |
|---|---|
| preconditions | 120.527 s |
| freeze_writes | 0.416 s |
| drain_to_home | 15.684 s |
| verify_data | 76.092 s |
| await_control_sync | 0.118 s |
| promote_home | 0.274 s |
| verify_traffic | 1.101 s |

## Settings used

```json
{
  "controller_tick_s": 60,
  "probe_timeout_s": 3.0,
  "scorer": {
    "window": 6,
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
