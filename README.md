# Intelligent Multi-Region Disaster Recovery System

B.Tech project by **Mahesh, Aarya and Dhrubajyoti**. A two-region (Mumbai `ap-south-1` → Singapore
`ap-southeast-1`) disaster recovery system with automatic failure detection, orchestrated failover,
recovery verification, human-approved failback, and evidence-based RTO/RPO measurement.

> **Current status (2026-10-18):** Option A is **deployed on real AWS** (Mumbai + Singapore) on the
> Free plan: $0.00 spend, $0 out-of-pocket. REAL-AWS drills passed: hard outage (write RTO 176 s,
> RPO 2.6 s), gray failure, health blip, and controlled failbacks with 0 writes lost. See
> `docs/07-real-aws-results.md`. The local simulation (labelled SIMULATED) still runs offline at $0.

## Quick start

**Double-click `Start-Demo.bat`** for the live dashboard, or **`DR-Menu.bat`** for every task (tests, drills, AWS status, teardown).

### Manual commands (Windows, Git Bash or PowerShell)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest              # 65 unit + 7 end-to-end tests (2026-10-18)
.venv/Scripts/python -m drsystem sim         # live system + dashboard at http://127.0.0.1:8100/
.venv/Scripts/python -m drsystem drill all --repeat 3 --failback   # drill suite -> evidence/simulated/
.venv/Scripts/python -m drsystem optimize --target-rto 300 --target-rpo 60   # MODEL ESTIMATE
```

## Share the demo online (Railway)

The `Dockerfile` runs the SIMULATED system with only the dashboard public on `$PORT`. It uses no AWS.

```bash
npm install -g @railway/cli
railway login
railway init
railway up
railway domain
```

Anyone with the URL can press the fault buttons. Redeploy (`railway up`) to reset the demo.

## What it demonstrates

| Capability | Where |
|---|---|
| Failure detection: independent prober + composite health score with hysteresis | `orchestrator/probe.py`, `orchestrator/scorer.py` |
| Safety guards: no failover to an unhealthy standby, cool-down, no automatic flap-back | `orchestrator/scorer.py` |
| Alert de-duplication: one alert per condition, reminders every `alert_repeat_s` (protects the SNS free tier) | `orchestrator/controller.py` |
| Recovery orchestration: lock → reassess → drain → promote → notify → verify | `orchestrator/workflows.py` (`FailoverWorkflow`) |
| Split-brain protection: epoch-fenced control object + peer fencing on writes | `control.py`, `app/service.py` |
| Recovery verification: reads and writes confirmed through the edge | `workflows.py` (`_verify_traffic`) |
| Controlled failback: approval, preconditions, write freeze, data verification, rollback | `workflows.py` (`FailbackWorkflow`) |
| RTO/RPO measurement under live traffic (sequence writer + user reader) | `drills/runner.py`, `drills/metrics.py` |
| Recovery-time optimizer: cheapest AWS config meeting targets (Always Free aware) | `drills/optimizer.py` |
| Evidence labelling: SIMULATED vs REAL-AWS on every event and report | `events.py`, `drills/report.py` |

## Local simulation vs real AWS

| Concern | Local (SIMULATED, now) | AWS (later, after gates) |
|---|---|---|
| Regional app | FastAPI process per "region" | Lambda + function URL per region (same code via Mangum) |
| Data + control object | `FileObjectStore` (one folder per region) | `aws/s3store.py`, S3 with conditional writes |
| Replication | `sim/replicator.py` (async, lag, LWW) | S3 two-way Cross-Region Replication |
| Edge failover | `sim/router.py` (CloudFront rules) | CloudFront origin group |
| Fault flags | in memory / dashboard buttons | `aws/faults_ssm.py`, SSM Parameter Store |
| Alerts | `notifications.jsonl` outbox | `aws/notify_sns.py`, SNS email |
| Controller schedule | background thread | EventBridge Scheduler → `aws/lambda_handlers.py` |
| Workflows | in-process | Step Functions state machines (same steps) |

The `drsystem.aws` package is never imported by the simulation and is tested only against `moto`
(offline mocks). `drsystem preflight` is the only command that talks to AWS, and it is read-only:
an allowlist hook blocks every non-read API call, including `UpgradeAccountPlan`.

## Zero out-of-pocket rules
See `docs/02-zero-cost-architecture.md`. In short: classic account on the Free plan, never upgrade,
never join AWS Organizations, no paid domains, deploy only after the read-only preflight passes,
and run the cleanup checklist after every deployment window.

## Documents
- `docs/00-implementation-plan.md`: original stage plan (partly superseded)
- `docs/01-architecture.md`: DR logic (control state, fencing, workflows, measurement)
- `docs/02-zero-cost-architecture.md`: Free-plan research, service and cost table, Options A/B
- `docs/03-local-implementation.md`: how the local implementation works, results, limitations
- `docs/04-aws-account-verification.md`: step-by-step account verification before any deployment
- `docs/05-account-verification-results.md`: verification, preflight and Gate 1 results
- `docs/06-deployment-proposal.md`: what was deployed and why
- `docs/07-real-aws-results.md`: **REAL-AWS deployment and drill results**
