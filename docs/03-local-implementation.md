# Local Implementation (Option A), SIMULATED

> Everything here runs on one machine, offline, at $0. Timings use the scaled-down profile
> `config/local-sim.toml` (probes every 0.5 s instead of 60 s, for example), so the numbers show that
> the **logic** works. They are **not** AWS RTO/RPO evidence.

## 1. Components

```
 browser / drill traffic
        │  reads: /api/*              writes: /r/<active-region>/*   (client-side write failover)
        ▼
 ┌────────────────────────── sim/router.py (CloudFront stand-in, :8100) ──────────────────────────┐
 │ origin group: GET/HEAD/OPTIONS try primary → on timeout/5xx retry secondary; writes no failover │
 └───────────────┬───────────────────────────────────────────────────────┬────────────────────────┘
                 ▼                                                       ▼
   app/api.py "ap-south-1" (:8101)                         app/api.py "ap-southeast-1" (:8102)
   RegionService: fencing, idempotency, health             RegionService
   FileObjectStore .sim/<run>/ap-south-1  ◀── sim/replicator.py (async, lag, LWW) ──▶  .../ap-southeast-1
                                                           DR control plane (in ap-southeast-1):
                                                             Controller.tick → HttpProber → HealthScorer
                                                             → FailoverWorkflow / FailbackWorkflow
                                                             → OutboxNotifier, EventLog (SIMULATED)
```

## 2. How recovery works
1. **Detect.** Every tick, the controller probes both regions' `/health`. The first failed probe is
   recorded (`t1`).
2. **Decide.** The scorer computes 0–100 from availability, latency and readiness. Failover happens after 3
   consecutive bad scores, or right away after 3 unreachable probes (fast path). It refuses if the
   standby is unhealthy, if the failing region isn't the home region, or during cool-down. Decision
   is recorded as `t2`. A refusal raises an **alert**. Alerts are de-duplicated on a stable code
   (`STANDBY_NOT_READY`, `NOT_HOME_REGION`, `COOLDOWN`) plus the region. A condition that persists is
   reminded only every `alert_repeat_s` (30 s in the simulation, 5 min on AWS). That protects the
   SNS free tier of 1,000 emails/month.
3. **Orchestrate.**
   - Take the lock.
   - Re-probe to rule out a false alarm.
   - Wait for replication to drain (bounded wait).
   - Promote via a conditional write of the control object (epoch + 1, `demoted_region` set, recorded as `t3`).
   - Notify.
4. **Verify.** Read `/api/whoami` through the edge until the target answers, and do a real write
   through the client until the target acknowledges it.
5. **Fence.**
   - The demoted region answers 503 to reads, so the edge moves traffic away from it.
   - Writes are accepted only where `active_region == me`.
   - Peer fencing rejects writes from a recovered region whose control object is still stale
     (`STALE_EPOCH`).
6. **Fail back (human-approved).** Preconditions (home healthy for N seconds, cool-down), then:
   - **write freeze**;
   - drain replication;
   - **verify data** (key + ETag match across regions);
   - wait until the control object has synced;
   - promote home;
   - verify traffic;
   - **automatic rollback** if verification fails.

## 3. Drill scenarios
| Scenario | Fault | Expected behaviour |
|---|---|---|
| `hard-outage` | primary unreachable, replication from it stops | failover, writes resume in DR, measure stranded writes (RPO) |
| `gray-errors` | 70% HTTP 500 at primary | failover via the score after hysteresis |
| `storage-down` | primary's store fails, app still up | failover (readiness drops) |
| `health-blip` | `/health` fails for 0.6 s | **no** failover (anti-flapping) |
| `standby-unhealthy` | DR unhealthy, then primary down | **no** failover, operator **alerted** |

Every failover drill can add `--failback`. That measures failback duration, how long writes are frozen, and
writes lost during failback (expected 0).

## 4. Measured quantities
Defined in `drills/metrics.py`:
- **Timing:** detection, decision, promotion, read RTO, longest user-visible read outage, write RTO.
- **Data loss:** lost acknowledged writes, RPO (data-loss window), late-arriving writes after the region recovers.
- **Latency:** read p50/p95 during the incident.

Reports are written to `evidence/simulated/<suite>/`:
- `SUMMARY.md` with medians and p90 across runs;
- one `.md` + `.json` per drill;
- `runs/*/events.jsonl` for raw timelines.

Every file is labelled SIMULATED.

## 5. Final results (2026-10-17), all SIMULATED

**Automated tests:** `python -m pytest` reports **66 passed, 0 failed, 0 skipped** (59 unit + 7 end-to-end).

**Drills:** 30 runs, **30 passed**. Every evidence file is labelled `SIMULATED`.

| Set | Runs | Passed | Write RTO (median) | Lost writes / RPO | Max read outage | Failback |
|---|---|---|---|---|---|---|
| `sample-suite` (5 scenarios × 3) | 15 | 15 | hard-outage 8.9 s · gray 4.3 s · storage 2.7 s | hard-outage 1–4 writes, RPO 0.30–0.40 s; others 0 | 0.0 s | 9/9 ok, 0 writes lost |
| `verification/gray-errors` | 10 | 10 | 4.4 s (3.3–5.0) | 0 / 0 s | 0.0 s | 10/10 ok, 0 lost |
| `verification/hard-outage` | 5 | 5 | 8.5 s (8.0–8.9) | 2–4 writes, RPO 0.08–0.34 s | 0.0 s | 5/5 ok, 0 lost |

Reading the numbers:
- **Hard-outage RPO** matches the simulated replication lag (0.3 ± 0.1 s), as expected: writes still
  in flight when the region dies are stranded.
- **Planned failback RPO** is 0 in every run (write freeze + data verification).
- **Health-blip and standby-unhealthy** never failed over. Standby-unhealthy alerted once, then
  reminded every `alert_repeat_s`.

Reports: `evidence/simulated/sample-suite/SUMMARY.md`, `evidence/simulated/verification/*/SUMMARY.md`.

## 6. Bugs found by drills and fixed
| Bug | Found by | Fix | Proven by |
|---|---|---|---|
| Alert flood: a persistent dual failure alerted every tick (reason text had a counter), which would exhaust the SNS free tier | dashboard dual-failure test | Stable alert code + `alert_repeat_s` reminders | `test_controller_alerts.py`; live run: 2 alerts in 35 s |
| A real gray failure dismissed: 2-probe reassess could see only successes (~9% at 70% errors), and the false alarm then armed the cool-down | `gray-errors` run in the suite (1/15 failed) | 3 reassess probes (~2.7%), **and** false alarms no longer arm the cool-down, so the controller re-triggers | `test_false_alarm_does_not_arm_cooldown`, `test_false_alarm_is_retried_not_dismissed` (forces a false alarm; failover still completes) |
| Drill runner stopped at the first (false-alarm) workflow and cut traffic too early | the forced-false-alarm test | Wait for the decisive workflow; retried outcomes are recorded | same test |
| RPO reported 0 s when one write was lost | suite review | RPO measured from the fault time back to the earliest lost write | `test_rto_rpo_computation` |

## 7. Known limitations (stated honestly in the report)
- **Partition split-brain window.** If both regions are alive but can't reach each other, each
  keeps its local view. Peer fencing allows writes when the peer is unreachable (availability over
  consistency). Closing this fully needs a quorum or third-party witness (e.g. ARC), which is out of
  budget.
- **Loss of the DR region itself.** The control plane runs in the DR region. If DR is down, nothing
  can fail over *to* it anyway, but nothing alerts either. Planned mitigation on AWS: a free
  CloudWatch alarm on the controller's heartbeat, evaluated from the home region.
- **Last-writer-wins replication.** Conflicting concurrent writes resolve by timestamp (as S3 CRR
  does). Fencing makes concurrent writers rare, but not impossible during a partition.
- **Simulated network.** Localhost latency is near zero. Real inter-region latency (~60 ms
  Mumbai↔Singapore), Lambda cold starts and real S3 replication lag will change the numbers. That is
  exactly what the REAL-AWS drills will measure.
- **Scaled timings.** The optimizer refuses to calibrate its AWS model with SIMULATED evidence.
- **Host contention.** One earlier (pre-fix) hard-outage run showed a 3.2 s read outage while the
  standby's own probes were slow, which points to laptop CPU contention, not logic. It did not recur
  in the 30 final runs (max read outage 0.0 s). A real-AWS drill is the only way to settle read
  behaviour under real CloudFront timeouts.
- **False alarms are reduced, not eliminated.** With 3 reassess probes a heavy gray failure can still
  look healthy (~2.7% at 70% errors). The fix is that this only delays failover by one detection cycle;
  it can no longer dismiss the failure.
- **The AWS adapters are untested against real AWS.** They're tested only against moto (offline mocks).
