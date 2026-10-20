# Drill report: `hard-outage`, **REAL-AWS**

> ✅ **REAL-AWS RESULT.** Measured against deployed AWS resources.

- **Drill ID:** `aws-hard-outage-6cf31c` (run `7d867a`)
- **Scenario:** Home app unreachable and home->DR replication paused (stranded writes).
- **Failed region:** `ap-south-1`, **target:** `ap-southeast-1`
- **Generated:** 2026-10-18 14:01:34 on Obsidian
- **Result:** ✅ PASSED

## Checks

| Check | Result | Observed |
|---|---|---|
| failover workflow completed | ✅ | `failed_over` |
| DR region holds write authority | ✅ | `ap-southeast-1` |
| writes resumed in DR | ✅ | `176.086` |
| failback completed | ✅ | `failed_back` |
| failback lost 0 writes (planned RPO = 0) | ✅ | `0` |

## Timeline (seconds after fault injection)

| Event | t + s |
|---|---|
| t1_first_failed_probe | 61.0 s |
| t2_decision | 121.0 s |
| t3_promoted | 166.8 s |
| alert | — |

## Recovery metrics

| Metric | Value |
|---|---|
| Detection (fault → first failed probe) | 60.991 s |
| Decision (first failed probe → failover decision) | 60.017 s |
| Promotion (decision → control object promoted) | 45.816 s |
| Read RTO (fault → first read served by target) | 3.807 s |
| Longest user-visible read outage | 0.0 s |
| Write RTO (fault → first write acked by target) | 176.086 s |
| Acknowledged writes lost at promotion | 3 |
| RPO (data-loss window) | 2.6 s |
| User read latency p50 during incident | 4387.2 ms |
| User read latency p95 during incident | 4768.0 ms |
| Lost writes that arrived late after the region recovered | 0 |

## Failover workflow steps

| Step | Duration | Details |
|---|---|---|
| reassess | 15.483 s | `{"source_failures": 3, "standby_ready": true, "confirmed": true}` |
| drain | 30.021 s | `{"source": "ap-south-1", "pending": 1, "oldest_pending_age_s": 30.009, "drained": false, "pending_keys_sample": ["replication-heartbeat/ap-south-1.json"]}` |
| promote | 0.114 s | `{"promoted": true, "epoch": 2, "demoted": "ap-south-1"}` |
| notify | 0.041 s | `{"notified": true}` |
| verify | 12.305 s | `{"reads_ok": true, "writes_ok": true, "target": "ap-southeast-1"}` |

## Controlled failback

- **Outcome:** `failed_back`, total 171.0 s, writes frozen for 48.0 s
- **Writes acknowledged during failback:** 138, **lost:** 0

| Step | Duration |
|---|---|
| preconditions | 122.99 s |
| freeze_writes | 0.366 s |
| drain_to_home | 17.72 s |
| verify_data | 28.69 s |
| await_control_sync | 0.128 s |
| promote_home | 0.292 s |
| verify_traffic | 0.796 s |

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
