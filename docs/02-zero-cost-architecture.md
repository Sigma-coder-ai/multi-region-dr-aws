# Zero Out-of-Pocket Architecture and Cost Model

**Status:** Draft for review. Nothing has been created in AWS.
**Date:** 2026-10-17
**Supersedes:** the cost model in `00-implementation-plan.md` and the deployment design in `01-architecture.md`. The DR logic in `01` (control object, fencing, failback, RTO/RPO measurement) still applies.

**Spending rule (mandatory):** if any step could need payment beyond eligible credits or free allowances, **stop and ask**. Never upgrade the account plan. Never assume someone will pay later.

---

## 1. What AWS's rules actually say (researched 2026-10-17)

### 1.1 There are now two different ways to sign up, and the difference is decisive for this project

| | **Sign up for AWS (advanced)**, the classic account | **Sign up for AWS (new)**, being rolled out to a limited number of customers |
|---|---|---|
| Login | Root user (email + password + MFA) | AWS Builder ID. **No root user** |
| Regions | You choose any Region | **One Region assigned by country. India → Sydney (`ap-southeast-2`)**. You can't change it |
| Multi-region | Allowed (subject to Free-plan service limits) | **"Multi-Region or cross-Region features are not supported for any services."** This holds even on the Paid plan, unless you upgrade *and* activate "advanced features", which can't be undone |
| Spend limit | Not available | Available on the Paid plan (from $20) |

**Consequence:** genuine multi-region DR is **impossible** on a "new experience" account without upgrading to Paid. This project needs a **classic ("advanced") account on the Free plan**.

**How to tell which one you have:**
- **Classic:** you sign in as root with an email address, and the console has a **Region selector** (top right).
- **New:** you sign in with an AWS Builder ID, there's a **Projects** menu, and you can see `settings.aws.com`.

### 1.2 Free plan vs Paid plan (classic account)

| Rule | Fact | Source |
|---|---|---|
| Credits | $100 at sign-up, plus up to $100 for completing 5 activities (EC2 launch, Lambda web app, Budgets, Bedrock playground, RDS database) | AWS Billing docs |
| Free plan length | Ends **6 months after sign-up or when credits run out, whichever comes first** | AWS Billing docs, Free Tier terms |
| Charges on the Free plan | **"No charges incur during usage."** When credits run out, the account **closes**; it doesn't bill your card | AWS Billing docs |
| After the plan ends | Account closed. Data kept for 90 days, then deleted unless you upgrade | AWS Billing docs |
| Restricted on the Free plan | Services "that could possibly deplete your credits", for example Marketplace, Savings Plans and Reserved Instances. **AWS publishes no complete list for classic accounts**, so we verify each service in the console | AWS Billing docs |
| Trials | The Free plan gets **only "Always Free" offers**. Short-term trials (such as GuardDuty's 30 days) are **not** active, so those services would consume credits | AWS Billing docs |
| ⚠️ **Automatic upgrade to Paid** | Happens if you **join AWS Organizations**, set up Control Tower, join the Partner Network, designate the account HIPAA/SEC, and a few others | AWS Billing docs, terms |
| Credits never cover | **Route 53 domain registration or transfer**, Marketplace, paid support plans, training/certification, upfront RI/Savings Plan fees, **taxes** | AWS Promotional Credit terms §1, §7 |
| Credit expiry | Free plan: when the plan ends (6 months). After an upgrade, remaining credits last until 12 months after sign-up | Free Tier FAQs |

### 1.3 ⚠️ Correction to my earlier advice
Earlier I told you to enable **IAM Identity Center "with AWS Organizations"**. **That would automatically upgrade your account to the Paid plan.** Don't do it.
- **Instead:** create **IAM users** (free) for Mahesh, Aarya, Dhrubajyoti and `aws-project-demo`, each with console access and **their own MFA device**. Put the three of you in an `AWS-Project` IAM group with AdministratorAccess, and put the demo user in a group with ReadOnlyAccess.
- **If you already enabled it:** check **Billing → Account plan** now and tell me what it shows.

### 1.4 Useful findings
- **Route 53:** up to **50 health checks on AWS endpoints are free**, for new and existing customers. Only optional features (HTTPS, string matching, fast interval) cost extra, at $1/month each. A hosted zone is $0.50/month.
- **CloudFront** gives a free `*.cloudfront.net` domain with HTTPS, and its always-free tier is large. So a working, public, failover-capable URL **needs no paid domain**.
- **Lambda function URLs** are free endpoints. Compared with API Gateway, there's no per-request fee and no public IPv4 to pay for.
- **Free-tier EC2 instance types** for accounts created on or after 2025-07-15: `t3.micro`, `t3.small`, `t4g.micro`, `t4g.small`, `c7i-flex.large`, `m7i-flex.large`. Their usage draws on credits.
- **Public IPv4** costs $0.005/hour per address, including idle Elastic IPs. The old "750 free hours" benefit isn't confirmed for the new plan, so we assume it doesn't apply.

---

## 2. Revised architecture principles

1. **Classic account, Free plan, no upgrade.** On the Free plan, out-of-pocket spending is $0 *by design*. The real risks are **running out of credits (the account closes)** and **the 6-month deadline**.
2. **Build locally first.** The 6-month clock starts at sign-up. **If the account isn't created yet, create it only when local development is finished.**
3. **Always-free services first, credits second.** Nothing runs that sits idle and costs money.
4. **No paid domain** (credits never cover domains). Use the free CloudFront domain. Optionally, a free student domain (see Option B).
5. **No always-on standby instances, no idle Elastic IPs, no NAT, no EKS, no Aurora, no paid monitoring, no Fault Injection Service.** FIS is Paid-plan-only in the new experience and not confirmed for the classic Free plan. We inject failures with our own tools instead.
6. **Real AWS time is spent in short, planned "deployment windows"**, with teardown after each.

---

## 3. AWS service table (every service either option might touch)

Legend: **AF** = Always Free allowance (available on the Free plan). **Credits** = covered by Free Tier credits. "Verify" = AWS publishes no classic-Free-plan list, so we confirm in the console at Gate 0 before use.

| # | Service | Purpose | Expected usage / month | Always-free allowance | Free plan available? | Credits apply? | Est. credit burn / month | Out-of-pocket |
|---|---|---|---|---|---|---|---|---|
| 1 | **IAM / STS** | Users for 4 people, roles for Lambda/EC2 | ~10 roles, 4 users | Free service | ✅ | n/a | $0 | $0 |
| 2 | **Lambda** | App API (A), prober, scorer, failover, failback, drill runner, credit watchdog | ~150k invocations, ~10k GB-s | 1M requests + 400k GB-s | ✅ (AWS activity) | ✅ | $0 | $0 |
| 3 | **Lambda function URLs** | Public HTTPS endpoint per region, with no API Gateway | 2 URLs | No extra charge beyond Lambda | ✅ | ✅ | $0 | $0 |
| 4 | **S3** (Standard) | App data, control object, web assets, drill evidence | <0.5 GB, ~50k PUT, ~200k GET | No always-free tier on the new plan | ✅ | ✅ | $0.10–0.40 | $0 |
| 5 | **S3 Cross-Region Replication** (two-way) | Genuine multi-region data replication | <0.2 GB replicated | None | ✅ verify (cross-region) | ✅ | <$0.05 | $0 |
| 6 | **Inter-region data transfer** | Replication traffic Mumbai ↔ Singapore | <0.2 GB | None | ✅ | ✅ | <$0.02 | $0 |
| 7 | **CloudFront** | Free public HTTPS URL; origin failover between regions; static web app | <1 GB, <100k requests | 1 TB + 10M requests + 2M Functions | ✅ | ✅ | $0 | $0 |
| 8 | **EventBridge Scheduler** | Prober and scorer every 1 min, watchdog daily | ~90k invocations | 14M invocations | ✅ | ✅ | $0 | $0 |
| 9 | **Step Functions** (Standard) | Failover, failback and drill workflows (visible audit trail) | <2,000 transitions | 4,000 transitions | ✅ | ✅ | $0 | $0 |
| 10 | **CloudWatch** metrics/alarms/dashboards | Health score, replication lag, RTO widgets | ≤10 custom metrics, ≤10 alarms, ≤3 dashboards | 10 metrics, 10 alarms, 3 dashboards, 1M API requests | ✅ | ✅ | $0 (if limits kept) | $0 |
| 11 | **CloudWatch Logs** | Lambda/EC2 logs, 7-day retention | <1 GB | 5 GB ingest + 5 GB storage | ✅ | ✅ | $0 | $0 |
| 12 | **SNS** | Email alerts to all 3 of you | <500 emails | 1M publishes, 1,000 emails | ✅ | ✅ | $0 | $0 |
| 13 | **SSM Parameter Store** (standard) | Thresholds, fault flags | <50 parameters | Standard tier is free | ✅ | n/a | $0 | $0 |
| 14 | **SSM Session Manager** (B) | Shell access to EC2 without SSH | A few sessions | Free | ✅ | n/a | $0 | $0 |
| 15 | **KMS** | AWS-managed keys only (no customer-managed keys at $1 each) | — | AWS-managed keys free; 20k requests | ✅ | ✅ | $0 | $0 |
| 16 | **CloudTrail** | Audit (event history) | Event history only | 90-day event history free | ✅ | n/a | $0 | $0 |
| 17 | **AWS Budgets** | Credit-burn alerts (secondary control) | 2 budgets | Budgets free; also **earns $20 credit** | ✅ | n/a | $0 | $0 |
| 18 | **Free Tier / Billing APIs** | Credit watchdog reads plan state and credit balance | 1 call/day | Free (verify the API at Gate 0) | ✅ verify | n/a | $0 | $0 |
| 19 | **Route 53 hosted zone** (optional) | Real DNS failover records | 1 zone | None | ✅ verify | ✅ (zones yes; **domains never**) | $0.50 | $0 |
| 20 | **Route 53 health checks** (optional) | Primary/DR health | 2 checks | **50 AWS-endpoint checks free** (basic) | ✅ verify | ✅ (HTTPS feature) | $0 basic / $2 with HTTPS | $0 |
| 21 | **Route 53 domain registration** | — | **Not used** | None | Unknown | ❌ **Never covered by credits** | — | **Would be out-of-pocket → excluded** |
| 22 | **EC2 `t4g.micro`** (B) | App server, primary; DR started only on failover (pilot light) | ~100 h primary + ~10 h DR | None on new plan (credits) | ✅ (AWS activity) | ✅ | $0.50–1.00 | $0 |
| 23 | **EBS gp3 8 GB** (B) | Instance root volume (**billed even while stopped**) | 1 volume always + DR volume only while running | None | ✅ | ✅ | $0.70–0.85 | $0 |
| 24 | **EBS snapshots / AMIs** (B) | Golden image copied to the DR region | ~2–3 GB actual data per region | None | ✅ | ✅ | $0.15–0.30 | $0 |
| 25 | **Public IPv4** (B) | Auto-assigned only while the instance runs. **No idle Elastic IPs** | ~110 h | Assume none | ✅ | ✅ | ~$0.55 | $0 |
| 26 | **VPC, subnets, IGW, security groups** (B) | Public subnet networking | 1 VPC per region | Free | ✅ | n/a | $0 | $0 |
| 27 | **Internet data transfer out** | Demo traffic | <1 GB | 100 GB/month free | ✅ | ✅ | $0 | $0 |

**Explicitly excluded** (cost, or blocked or unclear on the Free plan): NAT Gateway, Elastic IPs while idle, ALB/NLB, EKS, Aurora/RDS (except the one-time $20 activity, created and deleted immediately), API Gateway (replaced by function URLs), CloudWatch Synthetics, Fault Injection Service, GuardDuty/Security Hub/Inspector (no trial on the Free plan), customer-managed KMS keys, Secrets Manager, Route 53 domains, ARC, Shield Advanced, Marketplace AMIs, **AWS Organizations / IAM Identity Center (auto-upgrade to Paid)**.

---

## 4. Option A — Zero-burn serverless prototype (recommended first)

**Goal:** genuine two-region DR, built almost entirely from always-free services. **Credit burn ≈ $0.20–0.60/month. Out-of-pocket: $0.**

```
                 https://dxxxx.cloudfront.net   (free domain + free HTTPS)
                                │
                     ┌──────────▼──────────┐
                     │ CloudFront           │  /         → S3 web bucket (origin group: Mumbai → Singapore)
                     │                      │  /api/*    → origin group: Mumbai Lambda URL → Singapore Lambda URL
                     └───────┬────────┬─────┘  (fails over on 5xx or timeout, per request, GET/HEAD)
                             │        │
          ┌──────────────────▼──┐  ┌──▼───────────────────┐
          │ ap-south-1 (Mumbai) │  │ ap-southeast-1 (SG)  │
          │ Lambda URL: app API │  │ Lambda URL: app API  │   same code, same version
          │ S3: data + control ◀┼══┼▶ S3: data + control  │   two-way CRR
          └─────────────────────┘  │ DR control plane:    │
                                   │  prober (1 min), scorer, Step Functions │
                                   │  failover/failback/drill, SNS, dashboard│
                                   └─────────────────────────────────────────┘
```

**How failover works in A:**
- **Reads:** CloudFront retries the DR origin automatically when Mumbai returns 5xx or times out. Read RTO ≈ the origin timeout (seconds).
- **Writes:** CloudFront origin failover covers only GET/HEAD/OPTIONS. The web client sends writes to the active region:
  - **Normal case:** `/api/config` (which itself fails over) tells the client which region is active.
  - **On failure:** if a write gets a 5xx, timeout or `READ_ONLY`, the client refreshes the config and retries in the DR region.
  - **Fencing:** a region accepts writes only while the control object names it, as in `01-architecture.md` §3.
  - This "client-side failover" pattern is common in mobile and web apps.
- **Promotion:** the scorer in Singapore detects the failure (3 bad probes) and starts the Step Functions failover. It does a conditional write (`If-Match`) to the control object, which replicates to Mumbai. Expected write RTO ≈ 3–5 min.
- **Failback:** human-started, with preconditions, a write freeze and replication verification. RPO for a planned failback = 0.

**Optional A+ (credit burn +$0.50–2.50/month):** add a Route 53 hosted zone with **failover records** and 2 health checks. Without a registered domain, the zone isn't publicly delegated. But Route 53's DNS failover is still real and can be shown by querying the zone's assigned Route 53 name servers directly (`nslookup api.<zone> ns-xxx.awsdns-xx.com`). This is useful as an extra piece of evidence in the report.

**What's real vs simulated in A:** everything in AWS is real: two regions, replication, CloudFront failover, orchestration, measured RTO/RPO. The only gap compared with a production DNS setup is the missing public domain delegation.

---

## 5. Option B — Credit-funded EC2 deployment (still on the Free plan, no upgrade)

**Goal:** closer to a traditional primary/secondary setup with EC2, which your earlier requirement preferred. It's funded by credits, uses no always-on standby, and has **no idle IPv4**.

- **Primary:** `t4g.micro` in Mumbai, running **only during dev and demo windows**. A schedule stops it automatically after 6 h, and the watchdog enforces this.
- **DR, pilot light:** **no running instance**. A golden AMI is copied to Singapore with a launch template ready. On failover, a Lambda calls `RunInstances`. The measured boot-and-recover time is part of RTO.
- **Data:** the same S3 two-way replication and control object as A. The app code is shared: one FastAPI app runs on EC2 (uvicorn) and in Lambda (via the Mangum adapter).
- **Public routing.** Because the instance's IP changes on every start, the public name needs a DNS zone that the internet can resolve. Two choices:
  - **B1 (preferred): a free student domain.** The **GitHub Student Developer Pack** has historically included a free 1-year domain from partner registrars; we'll verify the current offer. Delegate it to a Route 53 hosted zone ($0.50/month from credits). Each instance registers its current IP at boot, and Route 53 failover records with **free basic HTTP health checks** route traffic. **Turn off auto-renewal on day 1.**
  - **B2 (no domain): CloudFront in front of EC2.** CloudFront needs a stable origin hostname, and without a domain the only stable name is an Elastic IP. That's 2 EIPs × $3.65/month even when idle, which goes against the "no unnecessary IPv4" rule, so **B2 is used only if B1 isn't possible and you approve it**.
- **Credit burn:** B1 ≈ **$2.50–4/month**; B2 ≈ **$10–13/month**. Out-of-pocket: **$0** while on the Free plan.
- **Worst case:** someone forgets to stop instances and both run 24/7 with EIPs: about $15–20/month. That's still within credits, but the watchdog stops instances after 6 h to prevent it.

**Real vs simulated in B:** everything is real AWS, including the **pilot-light recovery** (launching the DR instance from an AMI in another region), which is the main advantage of B over A.

---

## 6. Comparison and recommendation

| | A (serverless) | A+ (with Route 53) | B1 (EC2 + free domain) | B2 (EC2, no domain) |
|---|---|---|---|---|
| Genuine multi-region | ✅ | ✅ | ✅ | ✅ |
| Public URL | `*.cloudfront.net` | + undelegated zone | real domain | `*.cloudfront.net` |
| DNS failover shown | ❌ (CloudFront failover instead) | ✅ via direct queries to Route 53 | ✅ fully public | ❌ |
| EC2 / pilot-light depth | ❌ | ❌ | ✅ | ✅ |
| Read RTO (expected) | seconds | seconds / ~2 min DNS | ~3–8 min (boot) | ~3–8 min |
| Credit burn / month | ~$0.20–0.60 | ~$0.70–3.10 | ~$2.50–4 | ~$10–13 |
| Out-of-pocket | $0 | $0 | $0 (if the domain is truly free) | $0 |

**Recommendation:** build **A first** (most of the logic, cheapest). Then add **A+** and **B1** as short deployment windows for the demo. Over ~3 months of real AWS use, total credit burn should be **under $20**, well within the $100 sign-up credit.

---

## 7. Local-first development ($0, no AWS account needed)

| Layer | Local tool | Label |
|---|---|---|
| App + fencing + health | FastAPI app run as 2 processes (ports 8001 = "Mumbai", 8002 = "Singapore") | SIMULATED |
| S3 + replication | `moto` server for the S3 API, plus a small local replicator that copies objects with an artificial delay | SIMULATED |
| CloudFront / Route 53 failover | Local failover proxy that follows the same rules (5xx/timeout → secondary) | SIMULATED |
| Orchestrator | Same Python code as in AWS, with an in-memory clock for fast tests | SIMULATED |
| Terraform | `terraform validate`, `tflint`, `checkov`, plus plans against a mock provider config (no AWS credentials) | STATIC CHECK |
| Unit/integration tests | `pytest` + `moto` | SIMULATED |

**Simulated results never appear in the report as RTO/RPO evidence.** Only real AWS drills (T1/T2 in `01-architecture.md`) count.

---

## 8. Controls beyond Budgets

| Layer | Control |
|---|---|
| 1. Account plan | **Stay on the Free plan.** It's the real cap: AWS states that no charges are incurred, and the account closes instead of billing |
| 2. Upgrade triggers | Never join Organizations, enable Identity Center with Organizations, set up Control Tower, use Marketplace or click "Upgrade plan" |
| 3. Preflight script | Before every `terraform apply`, read-only checks: root MFA on, no root access keys, **account plan is still FREE**, credits remaining > $50, plan end date > 30 days away. Any failure → stop |
| 4. Credit watchdog Lambda | Daily: reads plan state and credit balance and emails all 3 of you. **Automatically stops** project EC2 instances after 6 h of runtime or if credits < $30 |
| 5. Budgets | "Credit burn" budget at $10/month, with credits **excluded** so it tracks gross usage. Alerts at 50/80/100%. Also earns a $20 credit |
| 6. Tagging | Every resource gets `Project=dr-system`, so a cross-region tag search finds everything |
| 7. Deployment windows | Planned windows (for example, 3 days). Then `terraform destroy` and the cleanup checklist |

---

## 9. Cleanup checklist (run after every deployment window)

1. `terraform destroy` for each stack, in reverse order (orchestrator → routing → regional → data). Do *not* destroy the bootstrap stack between windows.
2. **Run each of these checks in `ap-south-1`, `ap-southeast-1` and `us-east-1`:**
   - [ ] EC2 instances terminated (stopped instances still incur EBS charges)
   - [ ] EBS volumes: none left unattached
   - [ ] AMIs deregistered and their **snapshots deleted** (deregistering doesn't delete snapshots)
   - [ ] Elastic IPs released (idle EIPs are charged)
   - [ ] Lambda functions and function URLs deleted
   - [ ] Step Functions state machines and EventBridge schedules deleted
   - [ ] S3 buckets emptied **including all object versions and delete markers**, replication rules removed, buckets deleted
   - [ ] CloudWatch alarms, dashboards and log groups deleted
   - [ ] SNS topics and subscriptions deleted
   - [ ] SSM parameters deleted
3. **Global services:**
   - [ ] CloudFront distribution disabled, then deleted
   - [ ] Route 53 health checks deleted, records deleted, **hosted zone deleted** (charged monthly while it exists)
   - [ ] IAM roles created by Terraform deleted
4. **Verify:**
   - [ ] Resource Groups **Tag Editor**: search all regions for `Project=dr-system` → expect 0 results
   - [ ] `aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=dr-system` in each region → empty
   - [ ] Next day: Billing → Bills and Credits pages show no further growth in usage
5. **At the end of the project:** export code and evidence (already in git), delete the bootstrap state bucket, and decide whether to let the Free plan expire (the account closes automatically) or close it manually.

---

## 10. Gate 0 — verification before any AWS resource ($0, read-only, after you sign in)

1. **You, in the console:**
   - confirm a **classic** account (root login and a Region selector are present)
   - check **Billing → Account plan** = Free, and note the plan end date
   - check the **Credits** page: balance and expiry
   - check the **Free Tier** page
2. **Me, read-only CLI:**
   - account summary (root MFA on, no root keys)
   - enabled regions
   - free-tier-eligible EC2 types in both regions
   - Free Tier plan-state API (if available)
3. **You, in each planned service's console** (Lambda, S3 replication, CloudFront, Route 53, Step Functions, EC2): note any "upgrade required" banner.
4. **Gate 1 micro-test (needs approval, ~$0.00):** two empty buckets with replication in both regions, to prove cross-region replication is allowed on your plan. Deleted immediately afterwards.

**Prerequisites for any resource: root MFA done, and the preflight script passes.**

---

## Sources
- Choosing a Free Tier plan (AWS Billing docs): https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html
- Explore AWS services with Free Tier: https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier.html
- Earning additional credits: https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans-activities.html
- AWS Free Tier terms: https://aws.amazon.com/free/terms/
- AWS Free Tier FAQs: https://aws.amazon.com/free/free-tier-faqs/
- AWS Promotional Credit terms: https://aws.amazon.com/awscredits/
- Compare sign-up options: https://docs.aws.amazon.com/accounts/latest/reference/sign-up-for-aws.html
- Supported services for Sign up for AWS (new): https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html
- AWS Regions for your projects: https://docs.aws.amazon.com/accounts/latest/reference/project-regions.html
- Activate advanced AWS features: https://docs.aws.amazon.com/accounts/latest/reference/activate-advanced-features.html
- Route 53 pricing: https://aws.amazon.com/route53/pricing/
- Public IPv4 free-tier announcement (2024): https://aws.amazon.com/about-aws/whats-new/2024/02/aws-free-tier-750-hours-free-public-ipv4-addresses/
