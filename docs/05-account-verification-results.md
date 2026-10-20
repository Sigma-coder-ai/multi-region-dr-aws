# AWS Account Verification: Results (2026-10-18)

Method: read-only reading of console pages in the account owner's signed-in Chrome (Claude in
Chrome). Nothing was created, changed, upgraded or activated. The account ID is deliberately not
recorded here.

| Check | Result | Gate |
|---|---|---|
| Account type | **Classic** (root-user sign-in, Region selector present) | ✅ |
| Regions | Asia Pacific (Mumbai) `ap-south-1` and Asia Pacific (Singapore) `ap-southeast-1` both listed | ✅ |
| Account plan | **Free plan** ("Your free plan account does not get charged") | ✅ |
| Plan end | **Apr 18, 2027**, 183 days remaining (or earlier if credits run out) | ✅ (≥ 30 days) |
| Credits | **$100.00 remaining**, $0.00 used. One credit, "AWS Free Tier", issued 2026-10-18, listed expiry 2027-10-18 (the Free plan itself ends 2027-04-18) | ✅ (≥ $50) |
| Root MFA | "Root user has MFA" | ✅ |
| Root access keys | "Root user has no active access keys" | ✅ |
| IAM security recommendations | 0 open | ✅ |
| Cost anomaly monitor | 1 monitor active (AWS default), "None detected" | info |
| Budgets | none created yet | to do (free, earns a $20 credit) |
| Free Tier usage page | did not load ("Unable to load content"); normal for a brand-new account, retry within 24 h | pending |
| Explore AWS extra-credit activities | widget present; per-activity status not readable | pending (you check) |
| Per-service plan restrictions | not yet checked | pending (preflight + console) |
| Cross-region S3 replication allowed | **Yes**: Gate 1 micro-test PASS (see below) | ✅ |

**Banner buttons left untouched:** "Upgrade plan" (never click).

## IAM daily-use identity (verified read-only via CLI, 2026-10-18)
| Check | Result |
|---|---|
| IAM user | `Mahesh`, CLI profile `mahesh-project` (via `aws login`) |
| Group | `AWS-Project`: AdministratorAccess (plus redundant AdministratorAccess-AWSElasticBeanstalk and AdministratorAccess-Amplify, which can be detached) |
| MFA | 1 device |
| Access keys | 0 (none; good) |
| Same account as root profile | yes |

## Automated preflight: `drsystem preflight --profile mahesh-project`: **OK, all hard gates PASS**
| Check | Result |
|---|---|
| Plan | FREE / ACTIVE |
| Credits | **$120.00** (was $100; an Explore AWS activity credit has been added) |
| Plan expiry | 2027-04-18 (181 days) |
| Root MFA / root keys | enabled / none |
| AWS Organizations | not in use (no auto-upgrade risk) |
| Regions | ap-south-1 and ap-southeast-1 enabled |
| Lambda, Step Functions, Scheduler, SNS, SSM, CloudWatch | reachable in both regions |
| S3, CloudFront | reachable |
| **Lambda concurrency quota** | **10 per region** (new-account default) |

**Design impact of the Lambda quota of 10:** reserved concurrency can't be set (AWS keeps 10
unreserved), so the planned "reserved concurrency 20" cap is dropped. The account quota of 10 is
itself the cap. It also means a real drill must keep load small, or the regional app throttles (HTTP 429).

## Remaining before any deployment proposal
1. ✅ Done 2026-10-18: stale `[default]` key removed (backup: `~/.aws/credentials.bak-20261018`).
2. ✅ Done: root profile `dr-project` signed out (`aws logout`); use `mahesh-project` only.
3. ✅ Done 2026-10-18 (both free, no budget actions):
   - `My Zero-Spend Budget` (created by you): emails if the real cost after credits goes above $0.01.
   - `credit-burn`: $5/month, credits **excluded**; emails at 50% and 100% actual and at 100% forecast. Tracks how fast the free credits are used.
4. ✅ Gate 1 micro-test done and passed (see below).
5. ✅ Deployment proposal written: `docs/06-deployment-proposal.md` (awaiting your approval; nothing deployed).

## Gate 1 micro-test: REAL-AWS, 2026-10-18: ✅ PASS
Script: `scripts/gate1_replication_test.py --profile mahesh-project` (preflight-gated, auto-cleanup).
Evidence: `evidence/real-aws/gate1-20261018-122755/result.json` (label `REAL-AWS`).

| Check | Result |
|---|---|
| Two-way S3 Cross-Region Replication allowed on this Free-plan account | **Yes** |
| Mumbai → Singapore, 5 small objects | 5/5 replicated, observed lag **23.4–25.9 s** |
| Singapore → Mumbai, 5 small objects | 5/5 replicated, observed lag **21.7–22.4 s** |
| ETags preserved on replicas | yes |
| `IfNoneMatch` conditional write enforced by real S3 | yes |
| `IfMatch` conditional write enforced by real S3 | yes |
| Metadata round-trip (`written_region`) | yes |
| Epoch-fenced promotion: a stale epoch is rejected on real S3 | yes |
| Cleanup: buckets, versions, replication rules, IAM role deleted | yes; independently verified: 0 buckets, 0 `dr-gate1-*` roles, 0 resources tagged `Project=dr-system` in ap-south-1, ap-southeast-1 and us-east-1 |
| Plan after the test | FREE / ACTIVE, credits shown $120.00 (the balance refreshes about every 24 h; expected usage ≈ $0.00) |

Caveats: the lag was measured by 1 s polling, a few seconds after the rules were created, with only
10 objects. That's a first real data point, not a statistic. The 3 deployment drills will measure it properly.

**Model update:** with the measured lag (26 s), the optimizer still meets RTO ≤ 300 s and RPO ≤ 60 s at
$0/month (`evidence/model/optimizer-measured-lag.md`: write RTO about 161 s, hard-outage RPO about 26 s).
