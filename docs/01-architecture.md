# Architecture Design — Multi-Region DR System

**Team:** Mahesh, Aarya, Dhrubajyoti
**Status:** Draft v1 for team review. Nothing is deployed.
**Supersedes:** section 1 of `00-implementation-plan.md`

> ⚠️ **Deployment design superseded** by `02-zero-cost-architecture.md` (zero out-of-pocket). The DR logic here (control state, fencing, workflows, RTO/RPO measurement) still applies.

---

## 1. Goals and targets

| Goal | Target (v1) | Stretch |
|---|---|---|
| **RTO, reads** (fault → users served from the DR region) | ≤ 3 min | ≤ 60 s |
| **RTO, writes** (fault → writes accepted in the DR region) | ≤ 5 min | ≤ 90 s |
| **RPO** (acknowledged writes lost) | ≤ 5 s | ≤ 1 s |
| Failover decision | Fully automatic, no human in the loop | — |
| Failback | Human-approved, never automatic | — |
| Idle cost | ≤ $10/month | ≤ $5/month |
| Every number above | **Measured by drills**, not assumed | — |

**Non-goals for v1:** multi-account setup, relational databases (Aurora), containers/EKS, active-active writes.

---

## 2. Big picture

**Regions:**
- **Primary:** `ap-south-1` (Mumbai). Serves all traffic normally.
- **DR:** `ap-southeast-1` (Singapore). Fully deployed and scaled to zero. Takes over on failure.

**Strategy:** warm standby. Because everything is serverless, the DR stack is *fully deployed* but costs almost nothing until it receives traffic. You get warm-standby recovery speed at pilot-light cost.

```
                               ┌──────────────── CloudFront ────────────────┐
  Browser (demo dashboard) ───▶│ Origin group: S3 web bucket (primary) ──fail──▶ S3 web bucket (DR) │
          │                    └─────────────────────────────────────────────┘
          │ API calls
          ▼
  api.<domain> ──▶ Route 53 FAILOVER record
                    ├── PRIMARY → API GW custom domain (ap-south-1)   ◀─ HC-P: GET /health expects "active":true
                    └── SECONDARY → API GW custom domain (ap-southeast-1) ◀─ HC-D: GET /health expects "ready":true

 ┌──────────── ap-south-1 (PRIMARY) ────────────┐        ┌────────── ap-southeast-1 (DR) ──────────────┐
 │ API Gateway HTTP API                         │        │ API Gateway HTTP API                        │
 │  └ Lambda "api" (orders CRUD, /health, /whoami)│      │  └ Lambda "api" (same code, same version)   │
 │ DynamoDB replica: dr-app  ◀═════ Global Table (async, ~1 s) ═════▶ DynamoDB replica: dr-app  │
 │ SSM: /dr/fault/* (drill fault flags, regional)│       │ SSM: /dr/fault/*                            │
 │ S3 web + artifacts ───── Cross-Region Replication ─────▶ S3 web + artifacts                 │
 │ CloudWatch metrics/alarms                    │        │ ★ DR CONTROL PLANE (lives here on purpose) │
 └──────────────────────────────────────────────┘        │  • Prober Lambda (EventBridge Scheduler)    │
                                                         │  • Health scorer Lambda                     │
                                                         │  • Step Functions: failover / failback / drill │
                                                         │  • SNS alerts, CloudWatch dashboard         │
                                                         │  • Terraform state bucket                   │
                                                         └─────────────────────────────────────────────┘
```

**Key principle:** everything needed to *recover* lives in the DR region: the orchestrator, dashboard, alerts and even the Terraform state. If Mumbai goes down, nothing you need to fix the situation goes down with it.

---

## 3. The core idea: control state lives in the replicated data plane

This is the most important design decision. Read it carefully.

### The problem
Route 53 failover by itself has two flaws:
1. **Auto-failback flapping.** As soon as a failed primary passes its health check again, Route 53 sends traffic back, even if the primary is half-broken or its data is stale.
2. **Split-brain writes.** DNS moves traffic, but nothing tells the regions *which one is allowed to write*. DynamoDB Global Tables use last-writer-wins, so writes in two regions at once can silently overwrite each other.

AWS's official fix is Route 53 ARC routing controls, which cost about $1,800/month. Too expensive for a lab.

### Our solution: the `CONTROL#active-region` item
The DynamoDB global table holds one special item:

```json
{ "pk": "CONTROL", "sk": "active-region",
  "active_region": "ap-south-1",
  "epoch": 7,
  "changed_at": "2026-10-17T10:15:02Z",
  "changed_by": "sfn:failover/exec-abc123",
  "reason": "health score 22 < 40 for 3 evaluations" }
```

- **The `/health` endpoint reports two flags:**
  - `ready`: this region can serve requests (the DB is reachable and no fault is injected).
  - `active`: this region is the write authority (`active_region == my region`).
- **Route 53 checks a different flag in each region:**
  - The primary health check (HC-P) string-matches `"active":true`.
  - The DR health check (HC-D) string-matches `"ready":true`.
- **Writes are fenced:** the API rejects writes with `503 READ_ONLY` unless its own region is active.
- **Promotion** is a *conditional* update: `SET active_region = :dr, epoch = epoch + 1 IF epoch = :expected`. Two orchestrators can never both promote.

### Why this works
| Scenario | What happens |
|---|---|
| Primary region dies (hard failure) | HC-P fails → DNS moves to DR within seconds. DR serves **reads** right away and returns `READ_ONLY` for writes. The orchestrator in DR promotes DR, and writes resume. *(RTO for reads < RTO for writes. Both are measured.)* |
| Primary degraded (gray failure: slow, 5xx errors, lag) | HC-P might still pass. The orchestrator detects it from the health score and promotes DR. The control item replicates to Mumbai in about 1 s, so Mumbai's `/health` returns `"active":false`, HC-P fails, and DNS moves. One mechanism handles both cases. |
| Primary recovers | Its replica receives the control item (`active_region = DR`), so HC-P keeps failing and **no automatic flap-back happens**. |
| Failback | A human starts the failback workflow. It checks preconditions and then sets `active_region` back to the primary. DNS returns automatically. |

**No control-plane API calls happen during failover.** We never modify Route 53 records or health checks; only a DynamoDB write. This matters because Route 53's control plane lives in `us-east-1`, and failover must not depend on a third region.

**Known limitation (documented on purpose):** during a true network partition, a primary that is isolated but still running could accept writes from clients that reach it directly, until replication catches up. The fence closes the window to about one replication interval, but can't make it zero. ARC and quorum systems exist to solve exactly this. Drill D3 demonstrates it.

---

## 4. Application design

### 4.1 Sample workload: "Orders" API
It's simple, but it exercises everything DR cares about: writes, reads, idempotency and consistency.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/orders` | Requires an `Idempotency-Key` header. Fenced (only works in the active region) |
| `GET` | `/orders/{id}` | Served by any region |
| `GET` | `/orders?customer=…` | Uses the GSI (secondary index) |
| `PATCH` | `/orders/{id}` | Fenced. Optimistic locking via `version` |
| `GET` | `/health` | Deep check. Used by Route 53. Returns `{region, ready, active, epoch, checks:{db,fault}, version}` |
| `GET` | `/whoami` | Cheap. The dashboard polls it to show which region served the request |
| `POST` | `/drill/seq` | Internal (IAM auth). The RPO writer posts sequence numbers here |

### 4.2 Data model: single table `dr-app` (Global Table, on-demand billing)

| Entity | pk | sk | Key attributes |
|---|---|---|---|
| Order | `ORDER#<id>` | `META` | customer, items, total, status, version, written_region, written_at |
| Customer index | GSI1: `CUST#<cid>` | `ORDER#<ts>` | — |
| Control | `CONTROL` | `active-region` | active_region, epoch, changed_at, changed_by, reason |
| Failover lock | `CONTROL` | `lock` | owner, expires_at (TTL) |
| Health canary | `CANARY#<region>` | `META` | Read by `/health` |
| RPO sequence | `SEQ#<drill_id>` | `<seq:010d>` | written_region, acked_at |
| Drill result | `DRILL#<drill_id>` | `META` / `T#<event>` | Timeline events, config snapshot, RTO/RPO |
| Idempotency | `IDEMP#<key>` | — | Managed by Powertools. TTL 24 h |

Every write stamps `written_region` and `written_at`, so after any failover you can trace which region wrote what.

### 4.3 Lambda implementation
- **Runtime:** Python 3.12 on **arm64**, which is about 20% cheaper. Memory 256 MB, timeout 10 s.
- **Libraries:** AWS Lambda **Powertools** (the AWS-published public layer) for structured logs, metrics (CloudWatch EMF), tracing and idempotency.
- **Control state caching:** the control item is cached in memory for **2 s**, so most requests don't pay for a DynamoDB read. Writes always do a fresh *strongly consistent* read of the local replica before committing. This keeps fencing correct.
- **Concurrency cap:** reserved concurrency of 20 per region, plus API Gateway throttling (10 rps rate, 20 burst). This protects you from a surprise bill.

---

## 5. Detection: the "intelligent" part

### 5.1 Signals
| Signal | Source | Where it's read | Weight |
|---|---|---|---|
| S1: Route 53 HC-P status | Route 53 → CloudWatch (`us-east-1` only) | Scorer calls `GetMetricData` in `us-east-1` | 30 |
| S2: Independent probe of primary `/health` | **Prober Lambda in DR**, every 60 s (10 s in drill mode) | DR CloudWatch | 30 |
| S3: Primary API 5xx rate and p99 latency | API GW metrics in `ap-south-1` | Scorer, cross-region read. *Unreachable = worst score* | 25 |
| S4: DynamoDB `ReplicationLatency` primary→DR | CloudWatch | Scorer | 15 |

### 5.2 Health score and decision rules
- **Score** is 0–100 (100 = healthy), the weighted sum of the four signals. For example, S3 scores full marks below 1% 5xx and p99 < 1 s, then drops linearly to 0 at 20% 5xx or p99 > 5 s.
- **Fail over** when the score is **below 40 for 3 consecutive evaluations**. This hysteresis prevents flapping.
- **Fast path:** S2 failing 3 times in a row **and** S1 unhealthy triggers immediate failover, skipping the 3-evaluation wait.
- **Safety:** never fail over if the **DR** region isn't `ready`. Alert instead.
- **Cool-down:** after any failover or failback, no automatic action for 15 min.
- **Tuning:** all thresholds live in SSM parameters, so the optimizer can tune them without a redeploy.

### 5.3 Triggering
- **Scheduled:** EventBridge Scheduler → scorer every 60 s.
- **Event-driven:** alarm state changes on S2/S3 → EventBridge → start the failover workflow immediately, without waiting for the next tick.
- **Locking:** a DynamoDB lock item (`CONTROL#lock`, conditional write with TTL) ensures only one workflow runs at a time.

---

## 6. Workflows (Step Functions, Standard, in the DR region)

### 6.1 Failover
```
Start ─▶ RecordEvent(detected) ─▶ AcquireLock ──fail──▶ End(already running)
   ─▶ Reassess (fresh score + DR ready?) ──not confirmed──▶ RecordFalseAlarm ─▶ ReleaseLock ─▶ End
   ─▶ DrainWait (until ReplicationLatency < 2 s, or 10 s max)   ← trades RTO for RPO, tunable
   ─▶ Promote (conditional write: active_region=DR, epoch+1) ─▶ RecordEvent(promoted)
   ─▶ Notify (SNS email to all 3 of you)
   ─▶ VerifyTraffic (loop every 5 s, 5 min max: resolve api.<domain>, call /whoami, POST test write)
        ├─ region==DR and write OK ─▶ RecordEvent(serving) 
        └─ timeout ─▶ Notify(CRITICAL) 
   ─▶ ComputeRTO ─▶ ReleaseLock ─▶ End
```

### 6.2 Failback (started by a human)
Preconditions, all required:
- the primary has been `ready` for at least 10 min
- replication latency is below 2 s in both directions
- the cool-down has elapsed

Then: promote the primary → verify traffic → record → release the lock.

### 6.3 Drill (game day)
```
Snapshot config ─▶ Start traffic generator + RPO writer (sequence numbers every 200 ms)
 ─▶ Inject fault (one of: health-fault flag | Lambda concurrency=0 | FIS Lambda error/latency action)
 ─▶ Wait for the failover workflow to finish (or a 10 min abort timer)
 ─▶ Stop writer ─▶ Measure RPO (acked seqs missing from DR) ─▶ Revert fault
 ─▶ Save results under DRILL#<id> ─▶ Optional: auto-failback after approval
```

---

## 7. Measuring and optimizing recovery time

### 7.1 Timeline events recorded for every drill or real failover
| Event | Meaning |
|---|---|
| `t0 fault_injected` | Drill only |
| `t1 first_failed_probe` | |
| `t2 decision` | Score threshold crossed or fast path fired |
| `t3 promoted` | Control item written |
| `t4 dns_serving_dr` | First `/whoami` answered by DR |
| `t5 first_write_dr` | |

From these:
- **Detection** = t1 − t0
- **Decision** = t2 − t1
- **Promotion** = t3 − t2
- **DNS shift** = t4 − t3 (or t4 − t1 for a hard failure)
- **RTO, reads** = t4 − t0
- **RTO, writes** = t5 − t0
- **RPO** = time span of acked-but-missing sequence numbers

### 7.2 The optimizer
A Lambda reads all `DRILL#*` results and produces a report (JSON, markdown and a dashboard widget). For each setting it shows:

| Setting | Options | RTO effect | Cost effect |
|---|---|---|---|
| Route 53 health-check interval | 30 s / 10 s | detection −20 s | +$1/check/month, ~3× health traffic |
| Health-check failure threshold | 3 / 2 / 1 | detection −interval per step | more false-positive risk |
| Health-checker regions | default (~8) / 3 | none | fewer health requests (see §10) |
| DNS TTL | 60 / 30 / 10 s | DNS shift −TTL difference | more DNS queries (fractions of a cent) |
| Prober interval | 60 / 10 s | detection | Lambda GB-seconds |
| Scorer evaluations | 3 / 2 | decision −60 s | more false-positive risk |
| DrainWait | 10 / 2 / 0 s | promotion faster | worse RPO |
| Lambda pre-warming | off / provisioned concurrency | first-request latency | **hourly charge**, measure briefly |

It then recommends **the cheapest configuration that meets the RTO and RPO targets**, backed by the measured data. *Stretch:* send the drill reports to Amazon Bedrock for a plain-English post-incident summary (costs cents).

---

## 8. Demo experience
- **"DR Live Monitor" web page:** a static site on S3 behind CloudFront, with CloudFront origin failover to the replicated DR bucket.
  - **Header:** polls `/whoami` every 2 s and shows a big region badge that turns 🟢 Mumbai or 🟠 Singapore.
  - **Status row:** active region, epoch number, health score sparkline.
  - **Drills:** a "Start drill" button (only for the demo user's narrow permission) and a live RTO timeline of the last drill.
- **CloudWatch dashboard** in the DR region, so it stays visible while Mumbai is "down".

---

## 9. Security design
| Area | Decision |
|---|---|
| Human access | IAM Identity Center: `AWS-Project` group (Admin) and `AWS-Project-Demo` (ReadOnly + a narrow `StartExecution` permission on the drill workflow) |
| Terraform | Runs as the `dr-terraform-deploy` role: scoped policy, region condition (`ap-south-1`, `ap-southeast-1`, `us-east-1`), permission boundary |
| Lambda roles | One role per function, least privilege. The orchestrator can write only the `CONTROL` items, read metrics, publish to SNS and set fault flags. **It can't delete anything** |
| Public API | `/orders*` open but throttled (it's a demo). `/drill/*` uses IAM auth. CORS limited to the CloudFront domain |
| Data | DynamoDB and S3 encrypted with AWS-managed keys (free). TLS only. S3 Block Public Access; CloudFront reaches S3 through OAC (Origin Access Control) |
| Secrets | None needed in v1. If some appear later: SSM SecureString |
| Supply chain | Pinned provider versions, committed lockfile, `checkov` + `tflint` before every plan |
| Logs | Structured, no request bodies, 7-day retention |

---

## 10. Cost model (design-level; verified with infracost before every apply)

**A cost that's easy to miss:** Route 53 health checkers probe from many locations. At a 30 s interval with the default checker regions, each endpoint gets roughly **1–3 million health requests a month**. Every request hits API Gateway, then Lambda, then DynamoDB.

| Health-traffic choice | Estimated monthly cost |
|---|---|
| 2 endpoints, default checker regions, 30 s | ~$2–5 |
| 2 endpoints, **3 checker regions**, 30 s | ~$0.50–1.50 ← **v1 default** |
| 10 s interval | ~3× the above, plus $1/check for fast interval |

Mitigation: the in-memory cache means `/health` reads DynamoDB at most once every 2 s per Lambda instance. It's also a good real-world lesson that *monitoring has a cost*.

| Component | Est. monthly |
|---|---|
| Route 53 zone + 2 health checks (HTTPS + string match) | ~$3–4 |
| Health-check traffic (API GW + Lambda + DynamoDB) | ~$0.50–1.50 |
| App traffic, drills, scorer/prober Lambdas, Step Functions | <$1 |
| DynamoDB Global Table storage + replicated writes | <$0.50 |
| S3 + CRR + CloudFront | <$0.50 (CloudFront has a large always-free tier) |
| CloudWatch alarms (~10), logs, 1 dashboard | ~$1–2 |
| ACM certificates, API GW custom domains | $0 |
| **Total baseline** | **≈ $5–9/month** + domain (~$3–15/year) |
| During drills only | FIS ~$0.10/action-minute; optional canaries |

---

## 11. Terraform structure

```
infra/
├── bootstrap/                 # local state; run once
│   └── main.tf                # state bucket in ap-southeast-1 (DR!), deploy role, budget action
├── modules/
│   ├── global_data/           # DynamoDB global table (+ replica), S3 buckets + CRR
│   ├── regional_app/          # API GW, Lambda, custom domain + ACM cert, alarms, SSM fault params
│   ├── dns_failover/          # zone lookup, HC-P/HC-D, failover alias records
│   ├── dr_control/            # prober, scorer, Step Functions ×3, EventBridge, SNS, lock/control seed
│   ├── observability/         # dashboard, composite alarms, optional canaries (toggle)
│   └── web_frontend/          # S3 + CloudFront origin group + OAC
└── live/lab/
    ├── providers.tf           # aws.primary, aws.dr, aws.use1 (aliases), default_tags
    ├── main.tf                # regional_app ×2 (for_each over regions), wiring
    ├── variables.tf           # domain, regions, thresholds, toggles (canaries_enabled, drill_mode)
    └── backend.tf             # S3 backend in ap-southeast-1, use_lockfile = true
```

**Why the state bucket lives in the DR region:** if Mumbai goes down, you still need Terraform to work. Keeping state in the region you might lose is a classic DR mistake. We'll avoid it on purpose and explain why in an ADR.

**Deployment order:** `bootstrap` → `global_data` → `regional_app` (both regions) → `dns_failover` → `dr_control` → `observability` → `web_frontend`. Each step is its own approval gate.

---

## 12. Repository layout

```
D:\AWS Project\
├── docs/        00-plan, 01-architecture, adr/, runbooks/, drills/
├── infra/       (above)
├── app/
│   ├── api/            handler.py, fencing.py, health.py, models.py
│   ├── dr_control/     prober.py, scorer.py, steps/*.py, optimizer.py
│   └── shared/         control_state.py, timeline.py, config.py
├── web/         index.html, app.js (DR Live Monitor)
├── tests/       unit/ (pytest + moto, offline), integration/ (gated, hits AWS)
├── scripts/     drill.py, rpo_writer.py, load.py
└── .github/workflows/   ci.yml (lint, test, plan via GitHub OIDC — later)
```

**Suggested team split** (change it however you like):

| Person | Owns |
|---|---|
| Mahesh | `infra/`: Terraform, DNS, IAM, cost guardrails |
| Aarya | `app/api` + `web/`: orders API, fencing, health, demo page, tests |
| Dhrubajyoti | `app/dr_control` + `scripts/`: prober, scorer, workflows, drills, optimizer |

---

## 13. Decision records to write (ADRs)

| ADR | Decision |
|---|---|
| 001 | Serverless warm standby instead of EC2/EKS: near-zero idle cost |
| 002 | DynamoDB Global Tables instead of Aurora Global DB: cost, managed replication; accept last-writer-wins |
| 003 | Control state in the replicated data plane instead of ARC routing controls or Route 53 API flips |
| 004 | Orchestrator, dashboard and Terraform state in the DR region |
| 005 | Automatic failover, human-approved failback |
| 006 | Health-check cost vs. detection speed (3 checker regions, 30 s default) |

---

## 14. Open decisions for the team

1. **Domain:** buy a new one through Route 53 (which TLD?), use a domain you already own, or go without a domain (weaker demo: no DNS failover)?
2. **DR region:** Singapore `ap-southeast-1` (default) or Hyderabad `ap-south-2`? Hyderabad is closer but opt-in and has fewer services.
3. **Targets:** are the v1 RTO/RPO numbers in §1 right for your report or demo?
4. **Code hosting:** a private GitHub repo for the three of you? This enables CI with GitHub OIDC and needs the `gh` CLI.
5. **Workload:** is "Orders" fine, or would you rather use something closer to your interests (notes, URL shortener, chat)? The DR design doesn't change.
