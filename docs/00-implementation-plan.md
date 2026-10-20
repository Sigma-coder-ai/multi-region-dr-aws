# Multi-Region Disaster Recovery System on AWS — Implementation Plan

**Project:** Intelligent Multi-Region Disaster Recovery System with Automated Failover and Recovery-Time Optimization
**Status:** Planning only. No AWS resources have been created.
**Date:** 2026-10-17

> ⚠️ **Superseded in part:** cost model, Stage 1 identity advice (IAM Identity Center with Organizations auto-upgrades the account to Paid) and EC2/paid-domain choices are replaced by `02-zero-cost-architecture.md`.

---

## 0. What I found on your machine

| Item | Status | Notes |
|---|---|---|
| Working directory `D:\AWS Project` | Empty, not a git repo | This plan is the only file in it |
| AWS CLI | v2.36.16 ✅ | New enough for `aws login` |
| Terraform | ❌ Not installed | Needed |
| Git | 2.52 ✅ | |
| Python | 3.12.10 ✅ | We'll write the Lambda functions in Python 3.12 |
| Node / npm | 22.23 / 10.9 ✅ | Not required (useful for an optional frontend) |
| Docker | ❌ | Not required. The serverless design packages Lambdas as zip files |
| tflint, checkov, infracost, jq, gh | ❌ | Recommended linting, security-scan and cost-estimate tools |
| winget | ✅ | Can install the missing tools |
| `python3` alias | ⚠️ Broken | Points at a removed Python 3.14 install. Use `python` |
| AWS config: `[default]` | ⚠️ **Long-term access key (AKIA…)** that AWS **rejects as invalid** | See security note below |
| AWS config: `[profile aws-agent]` | Uses `aws login`; **session expired** | Default region `ap-south-1` |
| AWS MCP server | Failed to connect (timeout) | Not needed for planning. Retry later if you want it |

> **Security note on `[default]`:** `~/.aws/credentials` holds a long-term access key that AWS no longer accepts. It may be an old or deleted key, or a key from another account. In Stage 1 you'll check in the IAM console whether that key still exists anywhere, especially as a **root** key. Then delete the stale entry from the credentials file. I have not changed this file.

---

## 1. Target architecture (learning-optimized, low idle cost)

The design is **serverless-first** on purpose. It shows real multi-region DR behaviour (replication, health checks, automatic DNS failover, orchestrated recovery, measured RTO/RPO) and costs only a few dollars a month when idle. Services with hourly charges, like NAT Gateways, ALBs, Aurora and EKS, are left out. Section 9 lists them as optional upgrades with their prices.

**Regions (proposed):**
- **Primary:** `ap-south-1` (Mumbai). Already your configured default and closest to you.
- **DR:** `ap-southeast-1` (Singapore). Enabled by default and has broad service coverage.
- Alternative DR region: `ap-south-2` (Hyderabad). It is closer, but it is an **opt-in region** that you must enable, and it offers fewer services. Stage 3 confirms service availability either way.

```
                         ┌────────────────────────────┐
     Users ──HTTPS──▶    │ Route 53 (failover routing) │◀── Health checks (every 10–30s)
                         └──────┬───────────────┬──────┘
                       PRIMARY  │               │  SECONDARY
              ┌─────────────────▼───┐       ┌───▼─────────────────┐
              │ ap-south-1 (Mumbai) │       │ ap-southeast-1 (SG) │
              │ API Gateway HTTP API│       │ API Gateway HTTP API│
              │ Lambda: app + /health│      │ Lambda: app + /health│
              │ DynamoDB table ◀────┼─Global Tables (async)─▶ DynamoDB table │
              │ S3 bucket ──────────┼── Cross-Region Replication ──▶ S3 bucket │
              │ CloudWatch alarms   │       │ CloudWatch alarms   │
              └─────────┬───────────┘       └──────────┬──────────┘
                        │      EventBridge (alarm state changes)
                        ▼                              ▼
              ┌──────────────────────────────────────────────────┐
              │ DR Orchestrator (Step Functions + Lambda)        │
              │  1. Health score: alarms + canary + replication lag │
              │  2. Decide: wait / degrade / fail over (anti-flap) │
              │  3. Act: flip health switch / scale DR / notify (SNS) │
              │  4. Record timeline → DynamoDB "drill-results"   │
              │  5. RTO/RPO optimizer: compare runs, suggest tuning │
              └──────────────────────────────────────────────────┘
```

**What makes it "intelligent":**
1. **Composite health score.** Failover is decided from several signals: Route 53 health, 5xx rate, p99 latency, DynamoDB `ReplicationLatency` and synthetic checks. One blip doesn't trigger it.
2. **Anti-flapping and split-brain guards.** Failover needs N consecutive bad evaluations. Failback is manual or requires a cool-down plus a healthy primary.
3. **Recovery-time optimization.** Every failover, real or drilled, records timestamps for *detect → decide → act → DNS shift → first good response*. The optimizer compares runs and recommends settings: health-check interval, failure threshold, DNS TTL, alarm periods, pre-warming. Each recommendation shows its RTO gain and its cost.
4. **RPO measurement.** Write sequence-numbered records continuously during a drill. After failover, compare what reached the DR region with what was written to measure actual data loss.

**DR strategy learning path:** you'll build and measure several strategies, from cheapest to fastest:
`Backup & Restore` → `Pilot Light` → `Warm Standby` (main build) → `Active-Active` (optional stretch).

---

## 2. Stages, approval gates and costs

Every stage that can incur AWS charges is marked with a 🔒 **GATE**. I'll stop at each gate, show you the `terraform plan` and the cost estimate, and wait for your explicit "yes". Stages 0–4 cost nothing.

| # | Stage | Who acts | AWS cost | Gate |
|---|---|---|---|---|
| 0 | Local tooling | Me (with your OK) | $0 | Install approval |
| 1 | Account security hardening | **You** in the console (I give you a checklist) | $0 | — |
| 2 | Billing protection | **You** in the console, or me via CLI with your OK | $0 | — |
| 3 | Regional availability and quotas | Me (read-only CLI) | $0 | — |
| 4 | Architecture and repo scaffolding | Me (local files) | $0 | — |
| 5 | Terraform foundation (state bucket) | Me | ~$0.05/mo | 🔒 A |
| 6 | Application development and local tests | Me | $0 | — |
| 7 | Deploy primary region | Me | ~$0–1/mo | 🔒 B |
| 8 | Deploy DR region, global data and DNS failover | Me | ~$2–5/mo, plus a domain at ~$3–15/yr | 🔒 C |
| 9 | Orchestrator and observability | Me | ~$1–3/mo | 🔒 D |
| 10 | DR testing (game days) | Me and you | Cents per drill | 🔒 E |
| 11 | Optimization, documentation and teardown | Me | Ends at $0 after destroy | 🔒 F (destroy) |

---

### Stage 0 — Local tooling (no AWS cost)

**Goal:** a reproducible toolchain.

- Install with winget/pip: **Terraform** (≥ 1.10 for native S3 state locking), **tflint** (Terraform linter), **checkov** (IaC security scanner), **infracost** (cost estimate per `plan`), **jq**. Optionally **gh**, for GitHub CI later.
- `git init` this folder and add a `.gitignore` that excludes `*.tfstate*`, `.terraform/`, `*.tfvars` and `.env`. This prevents secrets in state files from leaking to git.
- Set up a Python virtualenv with `boto3`, `pytest` and `moto` (moto mocks AWS so tests cost nothing).

**Risk:** low. Installs only from official sources (HashiCorp, PyPI).

---

### Stage 1 — AWS account security (do this first, before anything else)

**Goal:** a compromised laptop or a leaked key must not be able to drain your account.

Console checklist (you do these; **I will never ask for passwords or MFA codes**):
1. **Root user:** enable MFA (prefer a hardware key or passkey). Confirm **no root access keys exist**. Delete any that do. Then stop using root for daily work.
2. **Check the stale AKIA key** in your `[default]` profile. In IAM → Users → Security credentials, and under the root user's "My security credentials", deactivate and delete it if it still exists. Then remove the `[default]` block from `~/.aws/credentials`.
3. **Create a daily-use identity.** Preferred: **IAM Identity Center** (free) with an `AdministratorAccess` permission set for you, plus MFA. Sign in to the CLI with `aws sso login` or `aws login`. This uses short-lived credentials and needs no stored keys. Your `aws-agent` profile already uses `aws login`; just re-authenticate when needed.
4. **Least-privilege deploy role** for Terraform, created in Stage 5. It will be scoped to the services this project uses, restricted to the two chosen regions plus `us-east-1` (Route 53/billing are global or us-east-1) via an `aws:RequestedRegion` condition, and protected by a **permission boundary** so it can't create admin roles.
5. **Account-level S3 Block Public Access:** ON.
6. **CloudTrail:** confirm Event History (free, 90 days). Optionally add one trail of management events to S3. The first copy of management events is free; you pay only cents of S3 storage.
7. **IAM Access Analyzer** (external-access analyzer, free) in each project region.
8. **Alternate contacts** (security and billing) and a strong, unique root email password.
9. Optional, with costs: **GuardDuty** has a 30-day free trial, then roughly $1–5/mo for a small account. **Security Hub** also has a trial, then per-check pricing. I recommend GuardDuty and skipping Security Hub for now.

> **Account plan check:** AWS accounts created since mid-2025 start on a **Free plan** (credits, some service restrictions, automatic closure after the free period unless upgraded) or a **Paid plan**. Check which one you're on under Billing → Account plan. If something we need is blocked on the Free plan, I'll flag it before we hit it.

---

### Stage 2 — Billing protection (free)

**Goal:** you hear about any charge within hours, not at the end of the month.

1. **AWS Budgets** (monthly cost):
   - `dr-project-soft`: **$10**, alerts at 50%, 80% and 100% of actual spend, plus 100% of forecast.
   - `dr-project-hard`: **$25**, alert at 100% actual. Optionally attach a **Budget Action** that applies a deny-all IAM policy to the deploy role, which acts as an automatic brake.
2. **Cost Anomaly Detection** (free): service-level monitor with daily email alerts.
3. **Free Tier usage alerts:** enable in Billing preferences.
4. **CloudWatch billing alarm** in `us-east-1`. This is a backup to Budgets; the first 10 alarms are free.
5. **Tagging standard:** every resource gets `Project=dr-system`, `Env=lab`, `Owner=you`. Activate `Project` as a **cost allocation tag** so Cost Explorer can filter project spend.
6. **Kill switch:** `terraform destroy` per stack, plus a documented order for manual cleanup in the console.

---

### Stage 3 — Regional availability and quotas (read-only, free)

**Goal:** confirm that both regions support everything we need *before* writing code.

Read-only checks I'll run once you're signed in:
- `aws account list-regions` → which regions are enabled. Opt-in regions like Hyderabad need enabling.
- Per-region service availability for API Gateway HTTP APIs, Lambda (Python 3.12, arm64), DynamoDB Global Tables, S3 CRR, Step Functions, EventBridge, CloudWatch Synthetics and Fault Injection Service (FIS).
- **Service Quotas.** New accounts often get **low Lambda concurrency limits (sometimes 10)**, which matter for load tests. Also check API Gateway throttles and Route 53 health check limits. Request increases if needed (free).
- Measure latency from you to each region, so failover cost-of-latency is a known number.

**Output:** `docs/03-region-assessment.md` with a go/no-go for the region pair.

---

### Stage 4 — Architecture and repo scaffolding (free)

Proposed repository layout:

```
D:\AWS Project\
├── docs/                      # plans, ADRs (decision records), runbooks, drill reports
├── infra/
│   ├── bootstrap/             # state bucket + deploy role (local state, run once)
│   ├── modules/
│   │   ├── regional_app/      # API GW + Lambda + alarms (instantiated per region)
│   │   ├── global_data/       # DynamoDB Global Table + S3 CRR
│   │   ├── dns_failover/      # Route 53 zone, health checks, failover records
│   │   ├── orchestrator/      # Step Functions, EventBridge rules, SNS
│   │   └── observability/     # dashboards, composite alarms, canaries (toggle)
│   └── live/lab/              # root module: providers aliased per region, wires modules
├── app/
│   ├── api/                   # CRUD service (Python 3.12)
│   ├── health/                # deep health check (DB read/write, region, switch)
│   └── orchestrator/          # health scoring, decision, RTO/RPO recorder, optimizer
├── tests/                     # pytest + moto (offline), integration tests (gated)
├── scripts/                   # drill runner, RPO writer, teardown helpers
└── .github/workflows/         # optional CI with GitHub OIDC (no stored keys)
```

Design decisions get short **ADRs**, for example "Why DynamoDB Global Tables over Aurora Global DB" or "Why no ARC routing control cluster". That way you learn the trade-off along with the result.

---

### Stage 5 — Terraform foundation 🔒 GATE A

- **Bootstrap stack** (local state, run once):
  - S3 bucket for Terraform state: versioning ON, SSE encryption, Block Public Access, TLS-only policy. Terraform ≥ 1.10 locks with `use_lockfile = true`, so no DynamoDB lock table is needed.
  - `dr-terraform-deploy` IAM role with a scoped policy, region condition and permission boundary.
- **Conventions:** pinned provider versions with a committed `.terraform.lock.hcl`, `default_tags` on every provider, and two provider aliases (`primary` and `dr`) plus `us_east_1`.
- **Workflow on every change:** `fmt` → `validate` → `tflint` → `checkov` → `plan` → `infracost` → **your approval** → `apply`.

**Cost:** about $0.01–0.05/mo (state files are a few KB).
**Risk:** state can contain sensitive values. It is encrypted, private and never committed to git.

---

### Stage 6 — Application development (free, runs locally)

- **App:** a small "orders" REST API (`POST/GET/PUT/DELETE /orders`) on Lambda (Python 3.12, arm64, which is cheaper), with data in DynamoDB.
- **`/health` endpoint:** a *deep* check that does a DynamoDB read and write, reports the region and version, and honours a **health switch** in SSM Parameter Store (standard tier is free). The drills use this switch to simulate a failed region safely.
- **Idempotency keys** on writes, so retries during failover don't create duplicates.
- **Orchestrator Lambdas:** health scorer, decision engine (thresholds plus hysteresis), action executor, timeline recorder, optimizer report.
- **Tests:** unit tests with `moto`, and contract tests for the health endpoint. All run offline at $0.

---

### Stage 7 — Deploy primary region 🔒 GATE B

API Gateway HTTP API + Lambda + DynamoDB (on-demand) + S3 + CloudWatch logs and alarms in `ap-south-1`.

- **Safeguards:** API Gateway throttling (for example 20 rps burst / 10 rps rate), Lambda reserved concurrency caps, 7-day log retention, no public S3. These protect against "denial-of-wallet" attacks.
- **Cost:** about $0–1/mo at learning volumes. Mostly inside free tier and credits: CloudWatch alarms ~$0.10 each, logs ~$0.50–0.70/GB ingested.

---

### Stage 8 — DR region, global data and DNS failover 🔒 GATE C

- Deploy the same `regional_app` module to `ap-southeast-1`.
- Convert the table into a **DynamoDB Global Table** with replicas in both regions. Replicated writes are billed in each region (cents at our volume).
- **S3 Cross-Region Replication** with versioning; inter-region transfer runs ~$0.02–0.09/GB depending on the region pair.
- **Route 53:** hosted zone ($0.50/mo), 2 health checks (~$0.50–2/mo each, depending on HTTPS, string matching and fast-interval options), failover records with a low TTL (30–60 s).
- **Domain:** failover is easiest to show with a real domain. Registering one through Route 53 costs **~$3–15/year** depending on the TLD, is non-refundable and auto-renews unless you turn that off. **This is a separate purchase I'll ask you about explicitly.** Alternatives: a subdomain of a domain you already own, or a no-domain demo using a client-side or CloudFront origin-failover test. These are weaker, because they don't exercise DNS failover.

**Risk:** Global Tables use *last-writer-wins*. If both regions accept writes during a partial failure, conflicting updates can be silently overwritten. Mitigation: a single write region in warm standby, plus the orchestrator's split-brain guard.

---

### Stage 9 — Orchestrator and observability 🔒 GATE D

- EventBridge rules on alarm state changes → **Step Functions** (Standard; the first 4,000 transitions per month are free) → scorer, decision and action Lambdas → **SNS** email notifications (free tier).
- **CloudWatch dashboard** (first 3 free): health per region, replication latency, error rates, RTO history.
- **Synthetic canaries are a cost trap if left running.** At ~$0.0012 per run, one canary every 5 minutes is about **$10/month per region**. Plan: keep canaries **disabled by default** and enable them only during drills via a Terraform toggle. Use Route 53 health checks plus a lightweight scheduled Lambda probe for always-on monitoring.

---

### Stage 10 — Disaster recovery testing (game days) 🔒 GATE E

Each drill has a written hypothesis, success criteria, an abort plan and a report:

| Drill | How it's simulated | What it proves |
|---|---|---|
| D1 Soft failure | Flip the SSM health switch → `/health` returns 503 | Detection → DNS failover path, baseline RTO |
| D2 Compute failure | Set the primary Lambda's reserved concurrency to 0 | Real 5xx/throttle detection via alarms |
| D3 Fault injection | **AWS FIS** Lambda fault actions (injected errors/latency). About $0.10 per action-minute | Behaviour under partial or gray failure (anti-flap logic) |
| D4 Data RPO | Run the sequence writer, fail over, compare records | Measured RPO / replication lag |
| D5 Region evacuation | Full orchestrated failover plus **failback** | End-to-end runbook, failback safety |
| D6 Strategy comparison | Repeat with backup-restore / pilot-light / warm-standby configs | RTO-vs-cost curve for each strategy |

Every drill writes a timeline to the `drill-results` table. The optimizer then reports results like: *"Health-check interval 30s→10s cuts detection by ~20s, costs ~$1/mo more per check. TTL 60→30s cuts ~15s."*

**Safety:** drills run only in this lab account and only against project-tagged resources. FIS experiments have stop conditions tied to CloudWatch alarms.

---

### Stage 11 — Optimization, documentation and teardown 🔒 GATE F

- Final RTO/RPO report and architecture write-up (good portfolio material).
- **Teardown:** `terraform destroy` in reverse order (orchestrator → DNS → regions → data → bootstrap). Check Cost Explorer and the Billing console the next day to confirm $0 running cost. Remember the domain auto-renewal.

---

## 3. Cost summary

**Expected baseline while the lab is deployed:** about **$3–10/month**, mostly Route 53, CloudWatch and KMS/GuardDuty extras. Usage-based items stay in cents at learning volumes. New-account credits will likely cover it. **Outside the monthly baseline:** the one-time domain (~$3–15/year) and occasional drill costs (cents each).

| Item | Est. monthly | Notes |
|---|---|---|
| Route 53 hosted zone | $0.50 | |
| Route 53 health checks (×2) | $1–4 | Depends on HTTPS, string match and interval options |
| Lambda + API Gateway | ~$0 | Free tier covers learning traffic |
| DynamoDB on-demand + Global Table | <$1 | Replicated writes billed in each region |
| S3 + CRR | <$0.50 | |
| CloudWatch alarms, logs, dashboards | $1–3 | 7-day retention keeps logs cheap |
| Step Functions, EventBridge, SNS | ~$0 | |
| KMS customer-managed keys | $0 or $1/key/region | Default to AWS-managed keys, upgrade optionally |
| GuardDuty (after trial) | ~$1–5 | Optional |
| Synthetics canaries | **$0 off, ~$10/region on** | Drills only |
| FIS experiments | Cents per drill | |
| Domain (one-time/yearly) | $3–15/yr | Separate approval |

### ⚠️ Expensive services to avoid (or approve deliberately)

| Service | Approx. cost | Why it's tempting |
|---|---|---|
| **Route 53 ARC routing-control cluster** | **~$2.50/hour ≈ $1,800/month** | It's the "official" failover switch. We use Route 53 health checks plus Step Functions instead |
| **AWS Shield Advanced** | **$3,000/month**, 1-year commitment | DDoS protection. Never enable for a lab |
| Aurora Global Database | ~$200+/month minimum | Relational DR. Possible short-lived stretch goal |
| NAT Gateway | ~$32+/month **each**, plus $0.045/GB | Appears in most VPC tutorials. Our design needs no VPC |
| EKS control plane | ~$73/month per cluster | |
| Application Load Balancer | ~$16–25/month each | |
| Interface VPC endpoints | ~$7–10/month each per AZ | |
| Public IPv4 / Elastic IPs | ~$3.60/month each | |
| Lambda provisioned concurrency | Hourly while configured | "Pre-warming" optimization. Measure briefly, then remove |
| AWS Config recorder, Macie, Security Hub | Per-item / per-GB | Useful but adds up |

---

## 4. Security risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Stale or leaked long-term access key (found in `[default]`) | Full account takeover, crypto-mining bills | Delete it; use only short-lived credentials (Identity Center / `aws login`) |
| Root account without MFA | Total loss of account | Root MFA, no root keys, root used only for emergencies |
| Over-permissive Terraform role | One bug or mistake can create anything anywhere | Scoped policy, region condition, permission boundary |
| Terraform state exposure | State can hold ARNs, config and secrets | Private encrypted bucket, TLS-only, versioned, never in git |
| Secrets committed to git | Credential leak | `.gitignore`, no secrets in `.tfvars`, SSM Parameter Store SecureString |
| Public API abuse (denial-of-wallet) | Surprise bills | Throttling, reserved concurrency, budgets and anomaly detection. WAF optional (~$5+/mo) |
| Public S3 bucket | Data exposure | Account-level Block Public Access, bucket policies, checkov scans |
| Failover flapping / split-brain | Data conflicts, outage made worse | Hysteresis, single writer region, manual-approval failback |
| Automation with too much power | The orchestrator itself causes an outage | Its role can only flip the health switch, update DNS weights and scale. It can't delete anything |
| Fault-injection blast radius | Accidental impact outside the lab | FIS targets limited by `Project` tag, stop-condition alarms, lab account only |
| Supply chain (providers/modules) | Malicious code in infrastructure | Pinned versions, lockfile, official providers only, no unvetted community modules |
| Domain/DNS hijack | Traffic redirected | Registrar lock, MFA on account. DNSSEC optional |
| Logging sensitive data | PII in logs | Structured logs without request bodies, short retention |

---

## 5. Next actions

1. **You:** complete the **Stage 1** and **Stage 2** console checklists. I can walk you through each screen.
2. **You:** run `aws login --profile aws-agent` (or set up Identity Center), so I can run the free, read-only **Stage 3** checks.
3. **Me, after your OK:** install the Stage 0 tools and scaffold the repo (Stage 4). Nothing here touches AWS.
4. Nothing is created in AWS until you approve **GATE A** after reviewing the exact `terraform plan` and cost estimate.
