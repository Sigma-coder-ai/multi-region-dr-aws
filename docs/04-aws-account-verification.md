# AWS Account Verification (before any deployment)

**Purpose:** confirm your account type, plan, credits and regional service eligibility, using
**read-only** steps only. Nothing here creates resources, changes settings or costs money.
Until you complete it, your account status, type and credit balance are treated as **unverified**.

**Rules while verifying**
- Never paste passwords, MFA codes, access keys or session tokens into the chat. Share only the
  results (plan type, credit amount, dates, PASS/FAIL lines).
- **Don't click:** "Upgrade plan", "Create organization", "Enable IAM Identity Center",
  "Activate advanced features", any Marketplace subscription, or "Register domain".
- If any page offers a free trial or asks you to accept charges, close it and tell me.

---

## Step 1: Identify the account type (console, you)
Sign in at https://console.aws.amazon.com.

| You see | Account type | What it means for this project |
|---|---|---|
| Sign-in with **root user email**, and a **Region selector** in the top bar | **Classic ("advanced" sign-up)** ✅ | Multi-region possible on the Free plan (service checks still needed) |
| Sign-in with **AWS Builder ID**, a **Projects** menu, `settings.aws.com` | **New experience** ⚠️ | Locked to one Region (Sydney for India); multi-region needs a Paid upgrade. **Stop and tell me.** |

**Report back:** classic or new.

## Step 2: Plan, credits and dates (console, you)
Open **Billing and Cost Management → Home** (left menu: *Free Tier*, *Credits*).

| Item | Where | Write down |
|---|---|---|
| Account plan | Billing home, "Account plan" panel | `Free` or `Paid` |
| Plan end date | Billing home / Free Tier page | date |
| Credit balance | **Credits** page | amount remaining |
| Credit expiry | **Credits** page (per credit line) | date(s) |
| Extra-credit activities | Console Home → **Explore AWS** widget | which of the 5 are done |

**Expected:** Free plan, active, end date ≥ 30 days away, credits ≥ $50.
If the plan already says **Paid**, stop: a Paid account can bill your card beyond credits.

## Step 3: Root account hygiene (console, you)
- **IAM → Dashboard:** the security recommendations should show *root MFA enabled* and *no root access keys*.
- Account menu → **Security credentials**: confirm MFA is assigned and **Access keys = none**.
- Also delete the stale `[default]` key from `C:\Users\Asus\.aws\credentials` (it's invalid anyway).

## Step 4: A daily-use identity (console, you)
Create an **IAM user** for yourself (IAM → Users → Create user, console access, then assign MFA).
Do **not** use IAM Identity Center or Organizations (they auto-upgrade a Free-plan account).
Then sign in on the CLI (you do this; I never see the credentials):

```bash
aws login --profile aws-agent
```

## Step 5: Automated read-only preflight (CLI, I run it with your OK)
```bash
.venv/Scripts/python -m drsystem preflight --profile aws-agent
```
It calls only read APIs (an allowlist hook blocks everything else, including `UpgradeAccountPlan`):

| Check | API (read-only) | Gate |
|---|---|---|
| Who is calling (warns if root) | `sts:GetCallerIdentity` | — |
| Plan FREE + ACTIVE, credits, expiry | `freetier:GetAccountPlanState` | ✅ hard |
| Root MFA on, no root keys | `iam:GetAccountSummary` | ✅ hard |
| Not in an AWS Organization | `organizations:DescribeOrganization` | warn |
| `ap-south-1` and `ap-southeast-1` enabled | `account:ListRegions` | ✅ hard |
| Lambda reachable + concurrency quota (new accounts can be as low as 10) | `lambda:GetAccountSettings` | warn |
| Step Functions, Scheduler, SNS, SSM, CloudWatch reachable per region | `List*` / `Describe*` | warn |
| S3 and CloudFront reachable | `s3:ListBuckets`, `cloudfront:ListDistributions` | warn |

These read calls are free. Any hard-gate FAIL means **stop: no deployment proposal**.

## Step 6: Per-service plan check (console, you, 5 minutes)
In **each** of Mumbai and Singapore, open these consoles and note any banner like
"upgrade required" / "not available on your plan":
Lambda · S3 (Replication rules tab on any bucket page, *view only*) · CloudFront · Step Functions ·
EventBridge Scheduler · SNS · Systems Manager Parameter Store · CloudWatch.

## Step 7: What can't be verified read-only
**Cross-region S3 replication on your plan.** Only a tiny real test can prove it. This is
**Gate 1** and needs your explicit approval: two empty buckets with a replication rule, one test
object of a few bytes, then delete everything. Expected cost ≈ $0.00 (a few requests, a few bytes).

## Step 8: GitHub Student Developer Pack (separate)
Not assumed. Verify separately at https://education.github.com/pack. It only matters for
Option B1 (free domain). Option A needs no domain.

---

## What happens after verification
1. You send me the Step 1–2 results and the preflight output.
2. If every hard gate passes, I prepare a **deployment proposal** for review only: exact
   resources, per-resource cost estimate, credit impact, deployment window, and teardown plan.
3. Nothing is deployed until you approve that specific proposal.
