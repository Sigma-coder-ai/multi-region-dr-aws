# Deployment Proposal: Option A on real AWS (for review, NOT deployed)

**Status:** proposal only. Nothing in this document has been created.
**Requires:** your explicit approval of this specific proposal before anything is deployed.
**Account:** classic, Free plan, $120 credits, plan ends 2027-04-18 (see `05-account-verification-results.md`).
**Out-of-pocket:** $0. On the Free plan AWS doesn't charge your card; usage draws on credits.

## 1. What would be deployed
The same code that passes the local suite, using the `drsystem.aws` adapters.

| # | Resource | Region | Purpose | Est. credit burn / month |
|---|---|---|---|---|
| 1 | S3 bucket `dr-app-aps1` (versioned, private, SSE-S3) | ap-south-1 | notes, seq, control object | < $0.05 |
| 2 | S3 bucket `dr-app-apse1` (versioned, private, SSE-S3) | ap-southeast-1 | replica + DR control | < $0.05 |
| 3 | IAM role for S3 replication (scoped to the 2 buckets) | global | two-way CRR | $0 |
| 4 | Two S3 replication rules (both directions) | both | genuine multi-region data | < $0.02 transfer |
| 5 | Lambda `dr-app` + function URL (arm64, 256 MB) | both | regional API | $0 (Always Free) |
| 6 | Lambda `dr-controller` (tick every 1 min) | ap-southeast-1 | prober + scorer | $0 (Always Free) |
| 7 | EventBridge Scheduler schedule `rate(1 minute)` | ap-southeast-1 | drives the controller | $0 (14M free) |
| 8 | Step Functions state machines: failover, failback | ap-southeast-1 | orchestration + audit trail | $0 (4,000 transitions free) |
| 9 | SNS topic + your email subscription | ap-southeast-1 | alerts (deduplicated) | $0 (1,000 emails free) |
| 10 | SSM parameters `/dr/faults/<region>` (standard) | both | real fault injection for drills | $0 |
| 11 | CloudWatch log groups (7-day retention), ≤ 10 alarms, 1 dashboard | both | observability | $0 (within Always Free) |
| 12 | CloudFront distribution (default `*.cloudfront.net`) with an origin group | global | public URL + read failover | $0 (1 TB / 10M req free) |
| 13 | IAM execution roles for the Lambdas (least privilege) | global | — | $0 |

**Expected total:** about $0.10–0.50/month of credits, and $0 out-of-pocket.
**Not included:** Route 53 (optional A+ only), EC2, NAT, any domain, FIS, Synthetics, GuardDuty.

## 2. Account-specific constraint found by the preflight
**Lambda concurrency quota is 10 per region.** That means no reserved concurrency, and real drills must keep
traffic light (writer at most 5 req/s). Raising the quota through Service Quotas is free, but a new account
may take time to get approval. Not required for the lab.

## 3. How it would be deployed
- **Tooling:** Terraform isn't installed yet. Proposal: install Terraform locally (free) and add `infra/`
  modules, or use a small boto3 deploy script with the same tags and a matching teardown script.
  Either way: `plan` → cost check → your approval → `apply`.
- **Every resource tagged** `Project=dr-system`, so the cleanup checklist and tag search find everything.
- **Preflight runs first** and blocks the deploy if the plan isn't FREE/ACTIVE, credits are under $50, or
  the plan ends within 30 days.

## 4. Real-AWS drills after deployment (labelled REAL-AWS)
Gate 1 already measured S3 CRR lag at about 22–26 s on this account (`evidence/real-aws/gate1-*`).
`hard-outage` (SSM fault flag `outage`), `gray-errors`, `health-blip`, then a controlled failback.
Three runs each, at real timings: probe every 60 s, 2–3 unreachable probes for the fast path. Expect a write
RTO of roughly 2–3 minutes and an RPO bounded by the measured S3 replication lag. The optimizer is then
calibrated with these REAL-AWS medians.

## 5. Teardown
`destroy` (or the teardown script), then the cleanup checklist in `02-zero-cost-architecture.md` §9:
- empty all object versions, then delete the buckets;
- delete the CloudFront distribution (disable first), the schedules, Lambdas, roles and log groups;
- verify that a tag search returns 0 resources in both regions and us-east-1.

## 6. Decision needed from you
Approve or reject this proposal, and choose the tooling: Terraform or the deploy script.
