# Drill suite summary, **REAL-AWS**

> ✅ **REAL-AWS RESULT.** Measured against deployed AWS resources.

- **Drills run:** 7, **passed:** 7
- **Generated:** 2026-10-18 18:00:07

## Pass rate by scenario

| Scenario | Runs | Passed |
|---|---|---|
| gray-errors | 3 | 3 |
| hard-outage | 2 | 2 |
| health-blip | 2 | 2 |

## `gray-errors`: metrics across runs

| Metric | n | median | p90 | min | max |
|---|---|---|---|---|---|
| Detection (fault → first failed probe) | 3 | 50.967 s | 82.4 s | 35.213 s | 90.288 s |
| Decision (first failed probe → failover decision) | 3 | 300.0 s | 300.1 s | 179.896 s | 300.094 s |
| Promotion (decision → control object promoted) | 3 | 23.224 s | 24.4 s | 21.186 s | 24.663 s |
| Read RTO (fault → first read served by target) | 3 | 0.723 s | 2.0 s | 0.25 s | 2.269 s |
| Longest user-visible read outage | 3 | 0.0 s | 0.0 s | 0.0 s | 0.0 s |
| Write RTO (fault → first write acked by target) | 3 | 359.057 s | 404.1 s | 253.543 s | 415.375 s |
| Acknowledged writes lost at promotion | 3 | 0 | 0.0 | 0 | 0 |
| RPO (data-loss window) | 3 | 0.0 s | 0.0 s | 0.0 s | 0.0 s |
| User read latency p50 during incident | 3 | 176.0 ms | 203.9 ms | 175.8 ms | 210.9 ms |
| User read latency p95 during incident | 3 | 499.3 ms | 598.6 ms | 307.1 ms | 623.4 ms |
| Failback duration | 3 | 325.3 s | 500.7 s | 214.2 s | 544.5 s |
| Writes lost across all failbacks | 3 | 0 | | | |

## `hard-outage`: metrics across runs

| Metric | n | median | p90 | min | max |
|---|---|---|---|---|---|
| Detection (fault → first failed probe) | 2 | 41.974 s | 57.2 s | 22.956 s | 60.991 s |
| Decision (first failed probe → failover decision) | 2 | 60.02 s | 60.0 s | 60.017 s | 60.024 s |
| Promotion (decision → control object promoted) | 2 | 45.866 s | 45.9 s | 45.816 s | 45.916 s |
| Read RTO (fault → first read served by target) | 2 | 3.064 s | 3.7 s | 2.322 s | 3.807 s |
| Longest user-visible read outage | 2 | 0.0 s | 0.0 s | 0.0 s | 0.0 s |
| Write RTO (fault → first write acked by target) | 2 | 157.095 s | 172.3 s | 138.103 s | 176.086 s |
| Acknowledged writes lost at promotion | 2 | 2.5 | 2.9 | 2 | 3 |
| RPO (data-loss window) | 2 | 1.976 s | 2.5 s | 1.351 s | 2.6 s |
| User read latency p50 during incident | 2 | 4361.05 ms | 4382.0 ms | 4334.9 ms | 4387.2 ms |
| User read latency p95 during incident | 2 | 5043.5 ms | 5263.9 ms | 4768.0 ms | 5319.0 ms |
| Failback duration | 2 | 238.85 s | 293.1 s | 171.0 s | 306.7 s |
| Writes lost across all failbacks | 2 | 0 | | | |
